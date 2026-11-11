"""Oximetry analytics: artefact filter, desaturation detection, ODI / T90 / pulse metrics.

All results are ``derived``. Desaturation events are computed on demand and never stored.
Definition used (documented because oximetry indices vary between tools):

* Artefact filter (per contiguous recording): drop a sample if SpO2 jumps more than
  ``spo2_max_slope`` %/s from its predecessor, then drop valid runs shorter than ``min_valid_run_s``.
* Baseline for a sample = maximum valid SpO2 in the preceding ``baseline_window_s`` seconds.
* A desaturation starts at the first sample ≤ baseline − drop; the baseline is frozen at that
  moment. It lasts while samples stay ≤ frozen baseline − drop and ends at the first sample
  above that level or at a gap. It counts when it lasts ≥ ``min_duration_s``.
* ODI = desaturations per *valid* hour.
* Hypoxic burden (desaturation-based approximation, not the event-window method of Azarbarzin
  et al.): for every >= 3 % desaturation the area between its frozen baseline and SpO2, from where
  SpO2 left the baseline (searched back <= ``AREA_EXTEND_S``) until it recovers to baseline − 1 %
  (searched forward <= ``AREA_EXTEND_S``, stops at gaps); sum in %·min per valid hour.

Not clinical interpretation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from pap_analytics.analytics.nights import AHI_TYPES
from pap_analytics.analytics.results import AnalyticsError
from pap_analytics.analytics.timeparse import Context, day_window
from pap_analytics.model import ChannelData, Event, EventType, Metric, Provenance
from pap_analytics.store import Store

DEFAULTS: dict[str, float] = {
    "spo2_max_slope": 4.0,  # % per second
    "min_valid_run_s": 10.0,
    "baseline_window_s": 120.0,
    "desat_min_duration_s": 10.0,
    "association_window_s": 30.0,
}
MAX_LISTED = 100
AREA_EXTEND_S = 60.0  # how far a desaturation's area may extend beyond its >= drop core, each side


@dataclass
class OximetryResult:
    start: datetime
    end: datetime
    recorded_s: float
    valid_s: float
    metrics: dict[str, Metric]
    desaturations: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


def _m(value: float | None, unit: str, method: str = "") -> Metric:
    return Metric(value, unit, Provenance.DERIVED, method)


def filter_artefacts(spo2: ChannelData, max_slope: float = 4.0, min_valid_run_s: float = 10.0) -> np.ndarray:
    """Copy of the SpO2 values with artefacts set to NaN (see module docstring)."""
    v = spo2.values.astype(np.float64).copy()
    rate = spo2.rate_hz
    with np.errstate(invalid="ignore"):
        jump = np.abs(np.diff(v, prepend=np.nan)) * rate > max_slope
    v[jump & np.isfinite(v)] = np.nan
    ok = np.isfinite(v)
    min_len = int(np.ceil(min_valid_run_s * rate))
    i, n = 0, len(v)
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i
        while j < n and ok[j]:
            j += 1
        if j - i < min_len:
            v[i:j] = np.nan
        i = j
    return v


def _detect(
    v: np.ndarray, rate: float, drop: float, min_duration_s: float, window_s: float
) -> list[tuple[int, int, float, float]]:
    """(start_idx, end_idx_exclusive, baseline, nadir) of desaturations in an already filtered series."""
    n = len(v)
    win = max(1, int(round(window_s * rate)))
    out: list[tuple[int, int, float, float]] = []
    i = 0
    while i < n:
        if not np.isfinite(v[i]):
            i += 1
            continue
        prev = v[max(0, i - win) : i]
        prev = prev[np.isfinite(prev)]
        if len(prev) and v[i] <= prev.max() - drop:
            base = float(prev.max())
            j = i
            while j < n and np.isfinite(v[j]) and v[j] <= base - drop:
                j += 1
            if (j - i) / rate >= min_duration_s:
                out.append((i, j, base, float(v[i:j].min())))
            i = j
        else:
            i += 1
    return out


def desaturation_area(v: np.ndarray, rate: float, a: int, b: int, base: float) -> float:
    """Area (%·s) between ``base`` and SpO2 for the desaturation core ``[a, b)`` of filtered ``v``,
    extended back to where SpO2 left the baseline and forward until it is back within 1 %."""
    ext = int(round(AREA_EXTEND_S * rate))
    k = a
    while k > 0 and a - k < ext and np.isfinite(v[k - 1]) and v[k - 1] < base:
        k -= 1
    m = b
    while m < len(v) and m - b < ext and np.isfinite(v[m]) and v[m] < base - 1.0:
        m += 1
    seg = v[k:m]
    seg = seg[np.isfinite(seg)]
    return float(np.clip(base - seg, 0.0, None).sum()) / rate


def detect_desaturations(
    spo2: ChannelData, drop_pct: float = 3.0, min_duration_s: float = 10.0, baseline_window_s: float = 120.0
) -> list[Event]:
    """Desaturation events (provenance ``derived``) in ``spo2`` as given (no artefact filtering)."""
    found = _detect(spo2.values.astype(np.float64), spo2.rate_hz, drop_pct, min_duration_s, baseline_window_s)
    return [
        Event(
            spo2.start + timedelta(seconds=a / spo2.rate_hz),
            (b - a) / spo2.rate_hz,
            EventType.DESATURATION,
            f"desat>={drop_pct:g}%",
            Provenance.DERIVED,
        )
        for a, b, _, _ in found
    ]


def _thr(thresholds: Mapping[str, float] | None) -> dict[str, float]:
    return {**DEFAULTS, **{k: float(v) for k, v in (thresholds or {}).items() if k in DEFAULTS}}


def oximetry_summary(
    store: Store,
    device_id: str,
    start: datetime,
    end: datetime,
    thresholds: Mapping[str, float] | None = None,
) -> OximetryResult | None:
    """Metrics for ``[start, end)``; ``None`` when the window holds no valid SpO2."""
    if end <= start:
        raise AnalyticsError("end must be after start")
    t = _thr(thresholds)
    parts = store.channel_window(device_id, "spo2", start, end)
    pulses = store.channel_window(device_id, "pulse", start, end)
    recorded = sum(len(p.values) / p.rate_hz for p in parts)
    raw_valid = sum(float(np.isfinite(p.values).sum()) / p.rate_hz for p in parts)
    if raw_valid == 0:
        return None
    filtered = [filter_artefacts(p, t["spo2_max_slope"], t["min_valid_run_s"]) for p in parts]
    valid_s = sum(float(np.isfinite(f).sum()) / p.rate_hz for f, p in zip(filtered, parts, strict=True))
    notes = [f"artefact filter removed {raw_valid - valid_s:.0f} s of {raw_valid:.0f} s valid samples"]
    if valid_s == 0:
        return OximetryResult(
            start, end, recorded, 0.0, {}, [], notes + ["no SpO2 left after artefact filter"]
        )
    allv = np.concatenate([f[np.isfinite(f)] for f in filtered])
    rate = parts[0].rate_hz
    metrics: dict[str, Metric] = {
        "valid_pct": _m(100 * valid_s / recorded if recorded else None, "%", "valid / recorded seconds"),
        "spo2_mean": _m(float(allv.mean()), "%", "after artefact filter"),
        "spo2_p50": _m(float(np.median(allv)), "%", "after artefact filter"),
        "spo2_min": _m(float(allv.min()), "%", "after artefact filter"),
        "t90_min": _m(float((allv < 90).sum()) / rate / 60, "min", "time with SpO2 < 90 %"),
        "t90_pct": _m(100 * float((allv < 90).sum()) / len(allv), "%", "share of valid time < 90 %"),
        "t88_min": _m(float((allv < 88).sum()) / rate / 60, "min", "time with SpO2 < 88 %"),
    }  # fmt: skip
    events: dict[float, list[tuple[float, datetime, float, float, float]]] = {3.0: [], 4.0: []}
    areas: dict[datetime, float] = {}  # %·s per >= 3 % desaturation, keyed by start
    for drop in events:
        for p, f in zip(parts, filtered, strict=True):
            for a, b, base, nadir in _detect(
                f, p.rate_hz, drop, t["desat_min_duration_s"], t["baseline_window_s"]
            ):
                s = p.start + timedelta(seconds=a / p.rate_hz)
                events[drop].append((drop, s, (b - a) / p.rate_hz, base, nadir))
                if drop == 3.0:
                    areas[s] = desaturation_area(f, p.rate_hz, a, b, base)
    hours = valid_s / 3600
    metrics["odi3"] = _m(len(events[3.0]) / hours, "1/h", "desaturations >=3 % per valid hour")
    metrics["odi4"] = _m(len(events[4.0]) / hours, "1/h", "desaturations >=4 % per valid hour")
    metrics["desat3_count"] = _m(float(len(events[3.0])), "count")
    metrics["desat4_count"] = _m(float(len(events[4.0])), "count")
    metrics["hypoxic_burden"] = _m(
        sum(areas.values()) / 60 / hours, "%min/h",
        "area between baseline and SpO2 of >=3 % desaturations per valid hour "
        "(desaturation-based approximation, not event-window HB)",
    )  # fmt: skip
    # pulse, restricted to samples with valid SpO2 when the two channels are aligned
    pv = []
    for q in pulses:
        vals = q.values.astype(np.float64)
        for p, f in zip(parts, filtered, strict=True):
            if p.rate_hz == q.rate_hz and p.start == q.start and len(f) == len(vals):
                vals = np.where(np.isfinite(f), vals, np.nan)
        pv.append(vals[np.isfinite(vals)])
    pulse = np.concatenate(pv) if pv else np.empty(0)
    if len(pulse):
        metrics["pulse_mean"] = _m(float(pulse.mean()), "1/min")
        metrics["pulse_min"] = _m(float(pulse.min()), "1/min")
        metrics["pulse_max"] = _m(float(pulse.max()), "1/min")
        metrics["pulse_p95"] = _m(float(np.percentile(pulse, 95)), "1/min")
    else:
        notes.append("no valid pulse data")
    # association with device respiratory events
    resp = [e for e in store.events(device_id, start - timedelta(minutes=1), end) if e.type in AHI_TYPES]
    ends = [e.start + timedelta(seconds=e.duration_s) for e in resp]
    win = timedelta(seconds=t["association_window_s"])
    d3 = events[3.0]
    if d3:
        hit = sum(1 for ev in d3 if any(timedelta(0) <= ev[1] - x <= win for x in ends))
        method = f"ODI3 desaturations starting <= {t['association_window_s']:g} s after a device event end"
        metrics["desat_after_event_pct"] = _m(100 * hit / len(d3), "%", method)
    listed = sorted(d3, key=lambda x: x[1])
    if len(listed) > MAX_LISTED:
        notes.append(f"desaturation list capped to {MAX_LISTED} of {len(listed)} (>=3 %)")
    four = {e[1] for e in events[4.0]}
    desats = [
        {"start": s, "duration_s": dur, "baseline": base, "nadir": nadir, "depth": base - nadir,
         "area_pct_min": areas.get(s, 0.0) / 60,
         "meets_odi4": s in four, "type": EventType.DESATURATION.value, "provenance": "derived"}
        for _, s, dur, base, nadir in listed[:MAX_LISTED]
    ]  # fmt: skip
    return OximetryResult(start, end, recorded, valid_s, metrics, desats, notes)


def night_oximetry(store: Store, device_id: str, day: date, ctx: Context) -> dict[str, Any] | None:
    """Hook for ``night_summary``: oximetry of the therapy day as a plain dict (or None)."""
    lo, hi = day_window(day, ctx)
    res = oximetry_summary(store, device_id, lo, hi, ctx.thresholds)
    if res is None:
        return None
    return {
        "recorded_s": res.recorded_s, "valid_s": res.valid_s, "metrics": res.metrics,
        "desaturations": res.desaturations, "notes": res.notes,
    }  # fmt: skip
