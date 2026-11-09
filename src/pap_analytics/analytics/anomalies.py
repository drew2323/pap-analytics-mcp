"""Rule-based pointers to the parts of a night worth a closer look (no ML, no own event scoring).

Each rule is a plain, documented threshold (defaults in ``DEFAULTS``; override via
``[thresholds]`` in the config). Findings are ``derived``, ranked by severity (0–1, higher =
look first), capped at ``MAX_ANOMALIES`` and always lie inside the night's sessions. Rules:

* ``event_cluster`` – >= ``cluster_min_events`` apnea/hypopnea events within ``cluster_window_min``.
* ``large_leak`` – leak above ``large_leak_lps`` for >= ``leak_min_s``.
* ``desaturation_cluster`` – >= ``cluster_min_events`` desaturations (>= 3 %) within the window.
* ``low_spo2`` – SpO2 below ``spo2_low_pct`` (any length that survives the artefact filter).
* ``pressure_at_max`` – pressure at the device maximum for >= ``pressure_max_min`` minutes
  (maximum = ``max_pressure`` or, for ASV, ``epap + max_ps`` from the settings in effect).
* ``low_ventilation`` – 60 s mean minute ventilation below ``vent_low_ratio`` x target (ASV).
* ``mask_off_gap`` – gap between sessions of the night longer than ``gap_min``.
* ``metric_outlier`` – AHI / leak p95 / usage / ODI3 more than ``outlier_z`` standard deviations
  from the previous ``baseline_nights`` nights (needs >= ``baseline_min_nights``; also a minimum
  absolute difference per metric, so near-constant metrics do not trigger).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from pap_analytics.analytics.nights import AHI_TYPES, list_nights
from pap_analytics.analytics.oximetry import _detect, _thr, filter_artefacts, oximetry_summary
from pap_analytics.analytics.timeparse import Context, day_window
from pap_analytics.model import ChannelData, Event, Metric, Provenance
from pap_analytics.store import Store

MAX_ANOMALIES = 20
DEFAULTS: dict[str, float] = {
    "cluster_min_events": 3,
    "cluster_window_min": 10,
    "leak_min_s": 120,
    "spo2_low_pct": 90,
    "pressure_max_min": 5,
    "vent_low_ratio": 0.5,
    "vent_low_min": 3,
    "gap_min": 10,
    "outlier_z": 2.0,
    "baseline_nights": 14,
    "baseline_min_nights": 5,
}
# minimal absolute deviation for an outlier to matter, per metric
OUTLIER_FLOOR = {"ahi": 1.0, "leak_p95": 0.05, "usage_min": 30.0, "odi3": 1.0}
OUTLIER_UNIT = {"ahi": "1/h", "leak_p95": "L/s", "usage_min": "min", "odi3": "1/h"}


@dataclass
class Anomaly:
    kind: str
    start: datetime
    end: datetime
    severity: float
    reason: str
    metrics: dict[str, Metric]
    suggested_call: str
    notes: list[str] = field(default_factory=list)


def _d(value: float, unit: str, method: str = "") -> Metric:
    return Metric(float(value), unit, Provenance.DERIVED, method)


def _spans(mask: np.ndarray, rate: float, min_len_s: float) -> list[tuple[int, int]]:
    """Index ranges ``[i, j)`` where ``mask`` is True for at least ``min_len_s``."""
    out: list[tuple[int, int]] = []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    for a, b in zip(edges[::2], edges[1::2], strict=True):
        if (b - a) / rate >= min_len_s:
            out.append((int(a), int(b)))
    return out


def _at(p: ChannelData, idx: int) -> datetime:
    return p.start + timedelta(seconds=idx / p.rate_hz)


def _sat(x: float) -> float:
    """Smooth saturation 0 → 0, 1 → 0.63, large → 1 (keeps long/large findings distinguishable)."""
    return 1.0 - math.exp(-x)


def _sev(x: float) -> float:
    return round(min(1.0, max(0.0, x)), 3)


def _call(day: date, start: datetime, end: datetime, ctx: Context) -> str:
    a = (start - timedelta(minutes=1)).astimezone(ctx.tz).strftime("%H:%M")
    b = (end + timedelta(minutes=1)).astimezone(ctx.tz).strftime("%H:%M")
    return f"segment_stats(date='{day.isoformat()}', start='{a}', end='{b}')"


def _hhmm(ts: datetime, ctx: Context) -> str:
    return ts.astimezone(ctx.tz).strftime("%H:%M")


def _event_groups(events: list[Event], min_events: int, window_min: float) -> list[list[Event]]:
    """Greedy groups of respiratory events that start within ``window_min`` of the group's first."""
    evs = sorted((e for e in events if e.type in AHI_TYPES), key=lambda e: e.start)
    out: list[list[Event]] = []
    i = 0
    while i < len(evs):
        j = i
        while j + 1 < len(evs) and evs[j + 1].start - evs[i].start <= timedelta(minutes=window_min):
            j += 1
        if j - i + 1 >= min_events:
            out.append(evs[i : j + 1])
            i = j + 1
        else:
            i += 1
    return out


def _pressure_max(store: Store, device_id: str, day: date) -> float | None:
    cur = [s for s in store.settings(device_id) if s.valid_from <= day]
    if not cur:
        return None
    v = cur[-1].values
    if isinstance(v.get("max_pressure"), int | float):
        return float(v["max_pressure"])
    if isinstance(v.get("epap"), int | float) and isinstance(v.get("max_ps"), int | float):
        return float(v["epap"]) + float(v["max_ps"])
    return None


def _rolling_mean(v: np.ndarray, n: int) -> np.ndarray:
    """Mean over non-overlapping blocks of ``n`` samples, repeated per sample (NaN-aware)."""
    out = np.full(len(v), np.nan)
    for a in range(0, len(v), n):
        blk = v[a : a + n]
        if np.isfinite(blk).sum() >= n // 2:
            out[a : a + n] = np.nanmean(blk)
    return out


def _night_findings(
    store: Store, device_id: str, day: date, lo: datetime, hi: datetime, sessions: list[Any],
    t: Mapping[str, float], ctx: Context,
) -> list[Anomaly]:  # fmt: skip
    out: list[Anomaly] = []

    def add(kind: str, a: datetime, b: datetime, sev: float, reason: str, metrics: dict[str, Metric]) -> None:
        out.append(Anomaly(kind, a, b, _sev(sev), reason, metrics, _call(day, a, b, ctx)))

    events = [e for e in store.events(device_id, lo, hi) if lo <= e.start < hi]
    # event clusters
    for g in _event_groups(events, int(t["cluster_min_events"]), t["cluster_window_min"]):
        a = g[0].start
        b = max(e.start + timedelta(seconds=e.duration_s) for e in g)
        kinds: dict[str, int] = {}
        for e in g:
            kinds[e.type.value] = kinds.get(e.type.value, 0) + 1
        add("event_cluster", a, b, 0.3 + 0.7 * _sat(len(g) / 12),
            f"{len(g)} respiratory events between {_hhmm(a, ctx)} and {_hhmm(b, ctx)} "
            f"({', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))})",
            {"events": _d(len(g), "count")})  # fmt: skip
    # large leak
    for p in store.channel_window(device_id, "leak", lo, hi):
        v = p.values.astype(np.float64)
        with np.errstate(invalid="ignore"):
            mask = v > ctx.large_leak_lps
        for i, j in _spans(mask, p.rate_hz, t["leak_min_s"]):
            a, b = _at(p, i), _at(p, j)
            peak, mins = float(np.nanmax(v[i:j])), (j - i) / p.rate_hz / 60
            add("large_leak", a, b, 0.3 + 0.7 * _sat(mins / 10),
                f"leak above {ctx.large_leak_lps:g} L/s for {mins:.1f} min "
                f"({_hhmm(a, ctx)}–{_hhmm(b, ctx)}), peak {peak:.2f} L/s",
                {"leak_max": _d(peak, "L/s"), "duration": _d(mins, "min")})  # fmt: skip
    # oximetry
    ot = _thr(ctx.thresholds)
    spo2_parts = store.channel_window(device_id, "spo2", lo, hi)
    low: list[tuple[datetime, datetime, float]] = []
    desats: list[tuple[datetime, float]] = []
    for p in spo2_parts:
        f = filter_artefacts(p, ot["spo2_max_slope"], ot["min_valid_run_s"])
        with np.errstate(invalid="ignore"):
            mask = f < t["spo2_low_pct"]
        for i, j in _spans(mask, p.rate_hz, 0):
            low.append((_at(p, i), _at(p, j), float(np.nanmin(f[i:j]))))
        for a, b, _, _ in _detect(f, p.rate_hz, 3.0, ot["desat_min_duration_s"], ot["baseline_window_s"]):
            desats.append((_at(p, a), (b - a) / p.rate_hz))
    merged: list[list[Any]] = []  # merge low spans closer than 60 s
    for a, b, m in sorted(low):
        if merged and a - merged[-1][1] <= timedelta(seconds=60):
            merged[-1][1], merged[-1][2] = max(merged[-1][1], b), min(merged[-1][2], m)
        else:
            merged.append([a, b, m])
    for a, b, m in merged:
        secs = max(1.0, (b - a).total_seconds())
        add("low_spo2", a, b, 0.2 + 0.8 * _sat((t["spo2_low_pct"] - m) / 5 + secs / 300),
            f"SpO2 below {t['spo2_low_pct']:g} % between {_hhmm(a, ctx)} and {_hhmm(b, ctx)} "
            f"(minimum {m:g} %, {secs:.0f} s)",
            {"spo2_min": _d(m, "%"), "duration": _d(secs, "s")})  # fmt: skip
    ds = sorted(desats)
    i = 0
    while i < len(ds):
        j = i
        while j + 1 < len(ds) and ds[j + 1][0] - ds[i][0] <= timedelta(minutes=t["cluster_window_min"]):
            j += 1
        if j - i + 1 >= t["cluster_min_events"]:
            a = ds[i][0]
            b = ds[j][0] + timedelta(seconds=ds[j][1])
            n = j - i + 1
            add("desaturation_cluster", a, b, 0.3 + 0.7 * _sat(n / 12),
                f"{n} desaturations (>=3 %) between {_hhmm(a, ctx)} and {_hhmm(b, ctx)}",
                {"desaturations": _d(n, "count")})  # fmt: skip
            i = j + 1
        else:
            i += 1
    # pressure at device maximum
    pmax = _pressure_max(store, device_id, day)
    if pmax is not None:
        for p in store.channel_window(device_id, "pressure", lo, hi):
            v = p.values.astype(np.float64)
            with np.errstate(invalid="ignore"):
                mask = v >= pmax - 0.05
            for i, j in _spans(mask, p.rate_hz, t["pressure_max_min"] * 60):
                a, b = _at(p, i), _at(p, j)
                mins = (j - i) / p.rate_hz / 60
                add("pressure_at_max", a, b, 0.3 + 0.7 * _sat(mins / 30),
                    f"pressure at the device maximum ({pmax:g} cmH2O) for {mins:.0f} min "
                    f"({_hhmm(a, ctx)}–{_hhmm(b, ctx)})",
                    {"pressure_max": _d(pmax, "cmH2O"), "duration": _d(mins, "min")})  # fmt: skip
    # ventilation far below target (ASV: target_vent present)
    for mv in store.channel_window(device_id, "minute_vent", lo, hi):
        tv = [
            x
            for x in store.channel_window(
                device_id, "target_vent", mv.start, mv.start + timedelta(seconds=len(mv.values) / mv.rate_hz)
            )
            if x.rate_hz == mv.rate_hz and x.start == mv.start
        ]
        if not tv or len(tv[0].values) != len(mv.values):
            continue
        n = max(1, int(round(60 * mv.rate_hz)))
        m60, t60 = _rolling_mean(mv.values.astype(np.float64), n), tv[0].values.astype(np.float64)
        t60 = _rolling_mean(t60, n)
        with np.errstate(invalid="ignore"):
            mask = (t60 > 0) & (m60 < t["vent_low_ratio"] * t60)
        for i, j in _spans(mask, mv.rate_hz, t["vent_low_min"] * 60):
            a, b = _at(mv, i), _at(mv, j)
            ratio = float(np.nanmean(m60[i:j] / t60[i:j]))
            mins = (j - i) / mv.rate_hz / 60
            add("low_ventilation", a, b, 0.3 + 0.7 * _sat((1 - ratio) + mins / 30),
                f"minute ventilation about {100 * ratio:.0f} % of target for {mins:.0f} min "
                f"({_hhmm(a, ctx)}–{_hhmm(b, ctx)})",
                {"vent_to_target": _d(100 * ratio, "%")})  # fmt: skip
    # mask-off gaps between sessions
    for prev, nxt in zip(sessions, sessions[1:], strict=False):
        gap = (nxt.start - prev.end).total_seconds() / 60
        if gap > t["gap_min"]:
            add("mask_off_gap", prev.end, nxt.start, 0.2 + 0.8 * _sat(gap / 120),
                f"mask off for {gap:.0f} min ({_hhmm(prev.end, ctx)}–{_hhmm(nxt.start, ctx)})",
                {"gap": _d(gap, "min")})  # fmt: skip
    return out


def _outliers(
    store: Store, device_id: str, day: date, lo: datetime, hi: datetime, t: Mapping[str, float], ctx: Context
) -> list[Anomaly]:
    prior = list_nights(store, device_id, day - timedelta(days=int(t["baseline_nights"])), day, ctx)
    mine = next((r for r in prior if r.date == day), None)
    hist = [r for r in prior if r.date < day and r.sessions]
    if mine is None or len(hist) < t["baseline_min_nights"]:
        return []

    def odi(d: date) -> float | None:
        res = oximetry_summary(store, device_id, *day_window(d, ctx), ctx.thresholds)
        m = res.metrics.get("odi3") if res else None
        return m.value if m else None

    def pick(r: Any, name: str) -> float | None:
        if name == "ahi":
            m = r.ahi_device if r.ahi_device is not None and r.ahi_device.value is not None else r.ahi_derived
            return m.value
        if name == "leak_p95":
            return r.leak_p95.value if r.leak_p95 else None
        if name == "usage_min":
            return r.usage_min.value
        return odi(r.date) if r.has_oximetry else None

    out = []
    for name in OUTLIER_FLOOR:
        cur = pick(mine, name)
        base = [x for x in (pick(r, name) for r in hist) if x is not None]
        if cur is None or len(base) < t["baseline_min_nights"]:
            continue
        mean, sd = float(np.mean(base)), float(np.std(base, ddof=1))
        if sd <= 0 or abs(cur - mean) < OUTLIER_FLOOR[name]:
            continue
        z = (cur - mean) / sd
        if abs(z) <= t["outlier_z"]:
            continue
        word = "above" if z > 0 else "below"
        out.append(Anomaly(
            "metric_outlier", lo, hi, _sev(0.4 + 0.1 * (abs(z) - t["outlier_z"])),
            f"{name} {cur:.2f} {OUTLIER_UNIT[name]} is {abs(z):.1f} SD {word} the mean {mean:.2f} of the "
            f"previous {len(base)} nights",
            {name: _d(cur, OUTLIER_UNIT[name]), "baseline_mean": _d(mean, OUTLIER_UNIT[name]),
             "z": _d(z, "SD")},
            f"compare_periods(a_from='{(day - timedelta(days=len(hist))).isoformat()}', "
            f"a_to='{(day - timedelta(days=1)).isoformat()}', b_from='{day.isoformat()}', "
            f"b_to='{day.isoformat()}')",
        ))  # fmt: skip
    return out


def find_anomalies(
    store: Store, device_id: str, day: date, thresholds: Mapping[str, float] | None = None,
    ctx: Context | None = None,
) -> list[Anomaly]:  # fmt: skip
    """Ranked anomalies of one therapy day (most severe first, at most ``MAX_ANOMALIES``)."""
    ctx = ctx or Context()
    t = {**DEFAULTS, **{k: float(v) for k, v in (thresholds or ctx.thresholds).items() if k in DEFAULTS}}
    sessions = sorted(store.sessions(device_id, day, day), key=lambda s: s.start)
    if not sessions:
        return []
    lo = sessions[0].start
    hi = max(s.end for s in sessions) + timedelta(seconds=1)
    found = _night_findings(store, device_id, day, lo, hi, sessions, t, ctx)
    found += _outliers(store, device_id, day, lo, hi, t, ctx)
    found = [a for a in found if math.isfinite(a.severity)]
    found.sort(key=lambda a: (-a.severity, a.start, a.kind))
    return found[:MAX_ANOMALIES]
