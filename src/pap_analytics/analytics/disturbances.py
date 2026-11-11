"""Sleep-disturbance metrics beyond AHI: pulse responses, pulse rises, hypoxic burden, flow limitation.

Everything here is ``derived`` and split into two explicit groups:

* **measured** – phenomena read directly off a signal: pulse response to device respiratory events
  (PRR), rapid pulse rises, hypoxic burden (see ``oximetry``), flow-limitation episodes on the
  device's ``flow_limitation`` channel.
* **inferred** – interpretations of measured phenomena: *possible autonomic arousals* (pulse rises)
  and *RERA candidates* (flow limitation followed by a recovery breath). Neither is a scored
  arousal or RERA (no EEG; see ADR-010); they are never added to
  the device AHI.

Definitions (thresholds are ``DEFAULTS``, overridable via ``[thresholds]``):

* Pulse is used only where the SpO2 artefact filter keeps the sample (same sensor), within
  30–220 /min and without steps > ``PULSE_MAX_STEP`` /min between 1 s samples.
* **PRR** per device apnea/hypopnea: max pulse in ``[event end, end + PRR_RESPONSE_S]`` minus the
  median pulse in the ``PRR_BASELINE_S`` before event onset. ResMed hypopneas are point marks
  (duration 0); their onset is assumed ``HYPOPNEA_ASSUMED_S`` before the mark. Needs >= 50 % valid
  samples in both windows.
* **Pulse rise**: pulse reaches local baseline + ``pulse_rise_bpm`` (baseline = median of the
  preceding ``pulse_baseline_s``, >= half valid) having been <= baseline + half that within the
  last ``PULSE_RISE_WITHIN_S`` (rapid, not a drift); it ends when pulse falls below baseline + half
  the rise (baseline frozen at onset) and counts when it lasted >= ``pulse_rise_min_s``.
  Each rise is linked to what preceded it (device respiratory event, desaturation, flow-limitation
  episode, device arousal mark, within ``link_window_s``) or marked ``unexplained``.
* **Flow-limitation episode**: device ``flow_limitation`` >= ``fl_threshold`` for >= ``fl_min_s``
  (gaps of one sample bridged).
* **RERA candidate**: a flow-limitation episode not overlapping a device apnea/hypopnea (± 10 s) and
  ended by a recovery breath: max tidal volume in the ``RECOVERY_S`` after the episode
  >= ``rera_recovery_ratio`` x median tidal volume during it (minute ventilation if no tidal volume).

Indices are per hour of *valid* signal (pulse hours for pulse metrics, flow-limitation channel
hours for flow metrics) so nights with partial oximetry stay comparable. Not clinical interpretation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from pap_analytics.analytics.nights import AHI_TYPES
from pap_analytics.analytics.oximetry import _detect, _thr, filter_artefacts, oximetry_summary
from pap_analytics.analytics.results import AnalyticsError
from pap_analytics.analytics.timeparse import Context, day_window
from pap_analytics.model import ChannelData, Event, EventType, Metric, Provenance
from pap_analytics.store import Store

DEFAULTS: dict[str, float] = {
    "pulse_rise_bpm": 8.0,
    "pulse_rise_min_s": 3.0,
    "pulse_baseline_s": 60.0,
    "link_window_s": 15.0,
    "fl_threshold": 0.2,
    "fl_min_s": 10.0,
    "rera_recovery_ratio": 1.5,
}
PULSE_RANGE = (30.0, 220.0)
PULSE_MAX_STEP = 10.0  # /min between consecutive 1 s samples; larger steps are artefacts
PULSE_RISE_WITHIN_S = 15.0
PRR_BASELINE_S = 30.0
PRR_RESPONSE_S = 30.0
HYPOPNEA_ASSUMED_S = 10.0
RECOVERY_S = 16.0
AHI_OVERLAP_S = 10.0
MAX_LISTED = 200


@dataclass
class DisturbanceSummary:
    start: datetime
    end: datetime
    pulse_valid_s: float
    flow_limitation_s: float
    measured: dict[str, Metric]
    inferred: dict[str, Metric]
    findings: list[dict[str, Any]]
    event_responses: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


def _m(value: float | None, unit: str, method: str = "") -> Metric:
    return Metric(None if value is None else float(value), unit, Provenance.DERIVED, method)


def effective_thresholds(overrides: Mapping[str, float] | None = None) -> dict[str, float]:
    """Disturbance + oximetry thresholds: defaults overlaid with the (validated) overrides."""
    o = overrides or {}
    return {**_thr(o), **DEFAULTS, **{k: float(v) for k, v in o.items() if k in DEFAULTS}}


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _ts(ch: ChannelData, i: float) -> datetime:
    return ch.start + timedelta(seconds=i / ch.rate_hz)


# ---- pulse ---------------------------------------------------------------------------------


def clean_pulse(pulse: ChannelData, spo2_valid: np.ndarray | None = None) -> np.ndarray:
    """Pulse values with artefacts set to NaN (range, steps, and optionally SpO2 validity mask)."""
    v = pulse.values.astype(np.float64).copy()
    if spo2_valid is not None and len(spo2_valid) == len(v):
        v[~spo2_valid] = np.nan
    v[(v < PULSE_RANGE[0]) | (v > PULSE_RANGE[1])] = np.nan
    with np.errstate(invalid="ignore"):
        step = np.abs(np.diff(v, prepend=np.nan)) > PULSE_MAX_STEP
    v[step & np.isfinite(v)] = np.nan
    return v


def _rolling_median(v: np.ndarray, win: int) -> np.ndarray:
    """Median of the ``win`` samples *before* each index (NaN when fewer than half are valid)."""
    out = np.full(len(v), np.nan)
    if len(v) <= win:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(v, win)[:-1]  # windows[k] = v[k:k+win]
    ok = np.isfinite(windows).sum(axis=1) >= win / 2
    med = np.full(len(windows), np.nan)
    if ok.any():
        med[ok] = np.nanmedian(windows[ok], axis=1)
    out[win:] = med
    return out


def detect_pulse_rises(
    pulse: ChannelData, values: np.ndarray | None = None, rise_bpm: float = 8.0, min_s: float = 3.0,
    baseline_s: float = 60.0,
) -> list[dict[str, Any]]:  # fmt: skip
    """Rapid pulse rises (see module docstring) in ``values`` (default: ``pulse.values`` as given)."""
    v = pulse.values.astype(np.float64) if values is None else values
    rate = pulse.rate_hz
    base = _rolling_median(v, max(1, int(round(baseline_s * rate))))
    within = max(1, int(round(PULSE_RISE_WITHIN_S * rate)))
    out: list[dict[str, Any]] = []
    i, n = 0, len(v)
    while i < n:
        b = base[i]
        if not (np.isfinite(v[i]) and np.isfinite(b) and v[i] >= b + rise_bpm):
            i += 1
            continue
        recent = v[max(0, i - within) : i]
        recent = recent[np.isfinite(recent)]
        if not len(recent) or recent.min() > b + rise_bpm / 2:
            i += 1  # no rapid rise: already elevated or a slow drift
            continue
        j = i
        while j < n and np.isfinite(v[j]) and v[j] >= b + rise_bpm / 2:
            j += 1
        if (j - i) / rate >= min_s:
            k = i + int(np.argmax(v[i:j]))
            out.append({"start": _ts(pulse, i), "end": _ts(pulse, j), "duration_s": (j - i) / rate,
                        "baseline": float(b), "peak": float(v[k]), "rise": float(v[k] - b),
                        "peak_at": _ts(pulse, k)})  # fmt: skip
        i = j
    return out


def _event_span(e: Event) -> tuple[datetime, datetime]:
    """(assumed onset, end) of a device respiratory event (hypopnea marks have no duration)."""
    end = e.start + timedelta(seconds=e.duration_s)
    onset = e.start if e.duration_s > 0 else e.start - timedelta(seconds=HYPOPNEA_ASSUMED_S)
    return onset, end


def pulse_response(pulse: ChannelData, values: np.ndarray, event: Event) -> dict[str, Any] | None:
    """PRR of one device respiratory event, or None when either window lacks valid pulse."""
    onset, end = _event_span(event)
    rate = pulse.rate_hz

    def window(a: datetime, seconds: float) -> np.ndarray | None:
        i0 = int(np.ceil((a - pulse.start).total_seconds() * rate - 1e-9))
        i1 = i0 + int(round(seconds * rate))
        if i0 < 0 or i1 > len(values):
            return None
        w = values[i0:i1]
        w = w[np.isfinite(w)]
        return w if len(w) >= (i1 - i0) / 2 else None

    pre = window(onset - timedelta(seconds=PRR_BASELINE_S), PRR_BASELINE_S)
    post = window(end, PRR_RESPONSE_S)
    if pre is None or post is None:
        return None
    base = float(np.median(pre))
    return {"event_start": event.start, "event_type": event.type.value, "baseline": base,
            "peak": float(post.max()), "response": float(post.max()) - base}  # fmt: skip


# ---- flow limitation -------------------------------------------------------------------------


def flow_limitation_episodes(
    fl: ChannelData, threshold: float = 0.2, min_s: float = 10.0
) -> list[tuple[int, int]]:
    """Index ranges ``[a, b)`` where device flow limitation >= ``threshold`` for >= ``min_s``."""
    v = fl.values.astype(np.float64)
    mask = np.isfinite(v) & (v >= threshold)
    inner = np.flatnonzero(~mask[1:-1] & mask[:-2] & mask[2:]) + 1  # bridge one-sample gaps
    mask[inner] = True
    return [(a, b) for a, b in _runs(mask) if (b - a) / fl.rate_hz >= min_s]


def _aligned(parts: Sequence[ChannelData], ref: ChannelData) -> np.ndarray | None:
    for p in parts:
        if p.rate_hz == ref.rate_hz and p.start == ref.start and len(p.values) == len(ref.values):
            return p.values.astype(np.float64)
    return None


def recovery_ratio(volume: np.ndarray, a: int, b: int, rate: float) -> float | None:
    """max(volume after the episode) / median(volume during it); None if not computable."""
    during = volume[a:b]
    during = during[np.isfinite(during)]
    after = volume[b : b + int(round(RECOVERY_S * rate))]
    after = after[np.isfinite(after)]
    if not len(during) or not len(after) or np.median(during) <= 0:
        return None
    return float(after.max() / np.median(during))


# ---- summary ---------------------------------------------------------------------------------


def _near(t: datetime, spans: Sequence[tuple[datetime, datetime]], before_s: float, after_s: float) -> bool:
    """True when ``t`` lies in ``[span start - before_s, span end + after_s]`` of any span."""
    lo, hi = timedelta(seconds=before_s), timedelta(seconds=after_s)
    return any(a - lo <= t <= b + hi for a, b in spans)


def disturbance_summary(
    store: Store, device_id: str, start: datetime, end: datetime,
    thresholds: Mapping[str, float] | None = None,
) -> DisturbanceSummary:  # fmt: skip
    """Measured and inferred disturbance metrics for ``[start, end)`` (see module docstring)."""
    if end <= start:
        raise AnalyticsError("end must be after start")
    t = effective_thresholds(thresholds)
    notes: list[str] = []
    measured: dict[str, Metric] = {}
    inferred: dict[str, Metric] = {}
    findings: list[dict[str, Any]] = []
    events = [e for e in store.events(device_id, start - timedelta(minutes=1), end) if e.start < end]
    resp = [e for e in events if e.type in AHI_TYPES and e.start >= start]
    resp_spans = [_event_span(e) for e in resp]
    arousal_marks = [(e.start, e.start) for e in events if e.type == EventType.AROUSAL]
    link = t["link_window_s"]

    # flow limitation (device channel, every night with detailed data)
    fl_parts = store.channel_window(device_id, "flow_limitation", start, end)
    tv_parts = store.channel_window(device_id, "tidal_volume", start, end)
    mv_parts = store.channel_window(device_id, "minute_vent", start, end)
    fl_s = sum(float(np.isfinite(p.values).sum()) / p.rate_hz for p in fl_parts)
    fl_spans: list[tuple[datetime, datetime]] = []
    fl_above_s = 0.0
    rera: list[dict[str, Any]] = []
    for p in fl_parts:
        v = p.values.astype(np.float64)
        fl_above_s += float(np.count_nonzero(np.isfinite(v) & (v >= t["fl_threshold"]))) / p.rate_hz
        vol = _aligned(tv_parts, p)
        vol_name = "tidal_volume"
        if vol is None:
            vol, vol_name = _aligned(mv_parts, p), "minute_vent"
        for a, b in flow_limitation_episodes(p, t["fl_threshold"], t["fl_min_s"]):
            s, e = _ts(p, a), _ts(p, b)
            fl_spans.append((s, e))
            ratio = recovery_ratio(vol, a, b, p.rate_hz) if vol is not None else None
            item = {"kind": "flow_limitation", "basis": "measured", "start": s, "end": e,
                    "duration_s": (b - a) / p.rate_hz, "max": float(np.nanmax(v[a:b])),
                    "recovery_ratio": ratio}  # fmt: skip
            findings.append(item)
            pad = timedelta(seconds=AHI_OVERLAP_S)
            overlaps_ahi = any(a_ - pad <= e and s <= b_ + pad for a_, b_ in resp_spans)
            if not overlaps_ahi and ratio is not None and ratio >= t["rera_recovery_ratio"]:
                rera.append({"kind": "rera_candidate", "basis": "inferred", "start": s, "end": e,
                             "duration_s": (b - a) / p.rate_hz, "recovery_ratio": ratio,
                             "recovery_signal": vol_name, "support": []})  # fmt: skip
    fl_hours = fl_s / 3600
    if fl_parts:
        measured["flow_limitation_time_pct"] = _m(
            100 * fl_above_s / fl_s if fl_s else None, "%",
            f"time with device flow limitation >= {t['fl_threshold']:g}",
        )  # fmt: skip
        measured["flow_limitation_index"] = _m(
            len(fl_spans) / fl_hours if fl_hours else None, "1/h",
            f"episodes >= {t['fl_threshold']:g} for >= {t['fl_min_s']:g} s per hour of flow-limitation data",
        )  # fmt: skip
    else:
        notes.append("no flow_limitation channel in this window; flow metrics are missing")

    # oximetry: hypoxic burden (single definition in oximetry) + desaturations for linking
    ox = oximetry_summary(store, device_id, start, end, thresholds)
    desat_spans: list[tuple[datetime, datetime]] = []
    spo2_parts = store.channel_window(device_id, "spo2", start, end)
    spo2_ok: dict[datetime, np.ndarray] = {}
    for p in spo2_parts:
        f = filter_artefacts(p, t["spo2_max_slope"], t["min_valid_run_s"])
        spo2_ok[p.start] = np.isfinite(f)
        for a, b, _, _ in _detect(f, p.rate_hz, 3.0, t["desat_min_duration_s"], t["baseline_window_s"]):
            desat_spans.append((_ts(p, a), _ts(p, b)))
    if ox is not None and "hypoxic_burden" in ox.metrics:
        measured["hypoxic_burden"] = ox.metrics["hypoxic_burden"]

    # pulse
    pulse_valid_s = 0.0
    rises: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    for q in store.channel_window(device_id, "pulse", start, end):
        mask = spo2_ok.get(q.start)
        v = clean_pulse(q, mask if mask is not None and len(mask) == len(q.values) else None)
        pulse_valid_s += float(np.isfinite(v).sum()) / q.rate_hz
        if not np.isfinite(v).any():
            continue
        for r in detect_pulse_rises(q, v, t["pulse_rise_bpm"], t["pulse_rise_min_s"], t["pulse_baseline_s"]):
            s = r["start"]
            linked = [name for name, spans, before in (
                ("respiratory_event", resp_spans, 5.0), ("desaturation", desat_spans, link),
                ("flow_limitation", fl_spans, 0.0), ("device_arousal", arousal_marks, link),
            ) if _near(s, spans, before, link)]  # fmt: skip
            rises.append({"kind": "pulse_rise", "basis": "measured", **r,
                          "linked_to": linked or ["unexplained"]})  # fmt: skip
        for e in resp:
            pr = pulse_response(q, v, e)
            if pr is not None:
                responses.append(pr)
    for c in rera:
        if any(c["start"] <= r["start"] <= c["end"] + timedelta(seconds=link) for r in rises):
            c["support"].append("pulse_rise")
        if _near(c["end"], desat_spans, link, link):
            c["support"].append("desaturation")
    pulse_hours = pulse_valid_s / 3600
    if pulse_hours > 0:
        resp_values = [r["response"] for r in responses]
        rise = t["pulse_rise_bpm"]
        measured["prr_median"] = _m(
            float(np.median(resp_values)) if resp_values else None, "1/min",
            f"median pulse response to device apneas/hypopneas "
            f"({len(resp_values)} of {len(resp)} events evaluable)",
        )  # fmt: skip
        measured["prr_p90"] = _m(
            float(np.percentile(resp_values, 90)) if resp_values else None, "1/min", "90th percentile of PRR"
        )  # fmt: skip
        measured["prr_responsive_pct"] = _m(
            100 * sum(1 for x in resp_values if x >= rise) / len(resp_values) if resp_values else None, "%",
            f"share of evaluable events with PRR >= {rise:g} /min",
        )  # fmt: skip
        measured["pulse_rise_index"] = _m(
            len(rises) / pulse_hours, "1/h", f"rapid pulse rises >= {rise:g} /min per valid pulse hour"
        )  # fmt: skip
        measured["pulse_rise_mean_bpm"] = _m(
            float(np.mean([r["rise"] for r in rises])) if rises else None, "1/min",
            "mean peak rise above baseline",
        )  # fmt: skip
        unexplained = sum(1 for r in rises if r["linked_to"] == ["unexplained"])
        inferred["autonomic_arousal_index"] = _m(
            len(rises) / pulse_hours, "1/h", "possible autonomic arousals (pulse rises) per valid pulse hour"
        )  # fmt: skip
        inferred["respiratory_arousal_index"] = _m(
            (len(rises) - unexplained) / pulse_hours, "1/h",
            "possible arousals linked to a respiratory event, desaturation or flow limitation",
        )  # fmt: skip
        inferred["unexplained_arousal_index"] = _m(
            unexplained / pulse_hours, "1/h", "possible arousals without a respiratory trigger in the data"
        )  # fmt: skip
    else:
        notes.append("no valid pulse in this window; pulse metrics and inferred arousals are missing")
    if fl_hours:
        inferred["rera_candidate_index"] = _m(
            len(rera) / fl_hours, "1/h",
            "flow limitation ended by a recovery breath, outside device AHI events",
        )  # fmt: skip
        inferred["rdi_inferred"] = _m(
            (len(resp) + len(rera)) / fl_hours, "1/h",
            "(device apneas/hypopneas + RERA candidates) per hour; inferred, not a device RDI",
        )  # fmt: skip
    findings += rises + rera
    findings.sort(key=lambda x: x["start"])
    if len(findings) > MAX_LISTED:
        notes.append(f"findings capped to {MAX_LISTED} of {len(findings)}")
    if len(responses) > MAX_LISTED:
        notes.append(f"event responses capped to {MAX_LISTED} of {len(responses)}")
    return DisturbanceSummary(
        start, end, pulse_valid_s, fl_s, measured, inferred, findings[:MAX_LISTED], responses[:MAX_LISTED],
        notes,
    )  # fmt: skip


def night_disturbances(store: Store, device_id: str, day: date, ctx: Context) -> DisturbanceSummary | None:
    """Disturbance summary over the sessions of a therapy day; None when the day has no sessions."""
    sessions = store.sessions(device_id, day, day)
    if not sessions:
        return None
    lo, hi = day_window(day, ctx)
    return disturbance_summary(store, device_id, lo, hi, ctx.thresholds)


TREND_KEYS = (
    ("measured", "prr_median"), ("measured", "pulse_rise_index"), ("measured", "hypoxic_burden"),
    ("measured", "flow_limitation_time_pct"), ("measured", "flow_limitation_index"),
    ("inferred", "autonomic_arousal_index"), ("inferred", "unexplained_arousal_index"),
    ("inferred", "rera_candidate_index"), ("inferred", "rdi_inferred"),
)  # fmt: skip


def disturbance_trend(
    store: Store, device_id: str, day_from: date, day_to: date, ctx: Context | None = None
) -> dict[str, Any]:
    """One row per night with sessions: the comparable nightly disturbance metrics (value only;
    units and methods once in ``metrics``), plus mean per metric over the nights that have it."""
    ctx = ctx or Context()
    days = sorted({s.therapy_day for s in store.sessions(device_id, day_from, day_to)})
    rows: list[dict[str, Any]] = []
    meta: dict[str, dict[str, Any]] = {}
    for d in days:
        res = night_disturbances(store, device_id, d, ctx)
        if res is None:
            continue
        row: dict[str, Any] = {"date": d, "pulse_valid_h": res.pulse_valid_s / 3600,
                               "flow_limitation_h": res.flow_limitation_s / 3600}  # fmt: skip
        for group, key in TREND_KEYS:
            m = getattr(res, group).get(key)
            row[key] = m.value if m else None
            if m:
                meta.setdefault(
                    key, {"basis": group, "unit": m.unit, "provenance": "derived", "method": m.method}
                )
        rows.append(row)
    for key, info in meta.items():
        vals = [r[key] for r in rows if r.get(key) is not None]
        info["mean"] = float(np.mean(vals)) if vals else None
        info["nights"] = len(vals)
    notes = [] if rows else ["no nights with sessions in this range"]
    return {"device_id": device_id, "metrics": meta, "nights": rows, "notes": notes}
