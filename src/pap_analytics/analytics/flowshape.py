"""Per-breath inspiratory flow shape (AirwayLab NED engine and Glasgow Index methods).

All results are ``derived``, computed on the breaths of ``breathing.segment_breaths`` (one
segmentation for all per-breath metrics). Nothing here scores respiratory events: device events
stay the AHI reference (ADR-011); NED RERA candidates are ``inferred`` and are checked against
device events by the caller (``disturbances``).

Per breath, the inspiration is the raw flow between the zero crossings around the inspiratory
peak of the segmented breath. Methods follow AirwayLab ``lib/analyzers`` (``ned-engine.ts``,
``glasgow-index.ts`` = DaveSkvn/GlasgowIndex), with flow in L/min where upstream thresholds are
absolute:

* **NED** = (Qpeak − Q at 50 % Ti) / Qpeak × 100 %; **FI** = mean / peak inspiratory flow. A breath is
  *flow limited* when NED > ``ned_fl_pct`` or FI >= ``FI_FL``.
* **NED RERA candidate**: a run of ``RERA_MIN_BREATHS``–``RERA_MAX_BREATHS`` flow-limited breaths ended
  by a sigh (next breath Qpeak >= ``ned_rera_sigh_ratio`` × run mean). Upstream also accepts runs
  with a rising NED or a max NED > 34 % without a sigh; on the fixture nights those were followed by
  a pulse rise no more often than random times, so the sigh is required here (ADR-012).
* **Glasgow components** (flag per breath, reported as share of breaths): skew (left-half volume
  outside 45–55 %), spike (< 20 % of Ti above 90 % of peak), flat top (variance of the middle half
  < 0.75 (L/min)²), top heavy (> 40 % of Ti above 90 % of peak), multi peak (second peak after a dip
  > 1 L/min), no pause (inspiration starts < 0.4 s after the extrapolated end of the preceding
  expiration), rate (> 20 /min over 5 breaths), multi breath (preceding expiration never below
  −10 L/min), variable amplitude (variance of the last 5 peaks > 4 (L/min)²). Glasgow inspirations
  are bounded by the +5 L/min grey zone as upstream. **Index** = sum of the nine shares (0–9).
* **Pressure-support context**: pressure support reshapes inspiratory flow, so each breath carries
  the PS (``pressure`` − ``epap``, 0.5 Hz) at mid-inspiration and the summary is split PS = 0 / PS > 0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from pap_analytics.analytics.breathing import Breaths, segment_breaths
from pap_analytics.model import ChannelData, Metric, Provenance

DEFAULTS: dict[str, float] = {"ned_fl_pct": 20.0, "ned_rera_sigh_ratio": 1.5}
FI_FL = 0.85
RERA_MIN_BREATHS, RERA_MAX_BREATHS = 3, 15
MIN_INSP_SAMPLES = 8
MIN_PEAK_LPM = 0.1
GREY_UPPER_LPM, GREY_LOWER_LPM = 5.0, -10.0
PS_ON_CMH2O = 0.5
GLASGOW = ("skew", "spike", "flat_top", "top_heavy", "multi_peak", "no_pause", "inspir_rate",
           "multi_breath", "variable_amp")  # fmt: skip
# Components reported individually: those that passed validation on the fixture nights (ADR-012).
GLASGOW_REPORTED = ("skew", "spike", "multi_breath", "variable_amp")

@dataclass
class BreathShapes:
    """Shape metrics per breath, aligned with the ``Breaths`` they were computed from.

    ``valid`` marks breaths whose inspiration could be delimited; other arrays are NaN/False there.
    ``glasgow`` maps each component to a boolean flag per breath."""

    valid: np.ndarray
    qpeak_lpm: np.ndarray
    ned_pct: np.ndarray
    fi: np.ndarray
    glasgow: dict[str, np.ndarray]

    def flow_limited(self, ned_fl_pct: float = 20.0) -> np.ndarray:
        return self.valid & ((self.ned_pct > ned_fl_pct) | (self.fi >= FI_FL))

def _multi_peak(x: np.ndarray, bump: float = 1.0) -> bool:
    """Upstream state machine: after the first peak, a dip > ``bump`` followed by a rise > ``bump``."""
    first, looking = False, False
    last_max = low = 0.0
    for val in x:
        if not first:
            if val > last_max:
                last_max = val
            elif val < last_max:
                first = True
        elif not looking and last_max - val > bump:
            looking, low = True, val
        if looking:
            if val < low:
                low = val
            elif val > low + bump:
                return True
    return False

def _bounds(v: np.ndarray, peak: int, level: float, stop: int) -> tuple[int, int]:
    s = peak
    while s > 0 and v[s - 1] > level:
        s -= 1
    e = peak
    while e < stop and v[e] > level:
        e += 1
    return s, e

def breath_shapes(flow: ChannelData, br: Breaths) -> BreathShapes:
    """Shape metrics of every breath in ``br`` (segmented from ``flow``); see module docstring."""
    rate = flow.rate_hz
    raw = np.nan_to_num(flow.values.astype(np.float64)) * 60.0  # L/min, upstream thresholds
    n = len(br.start_s)
    valid = np.zeros(n, bool)
    qpeak, ned, fi = (np.full(n, np.nan) for _ in range(3))
    g = {name: np.zeros(n, bool) for name in GLASGOW}
    starts = np.rint(br.start_s * rate).astype(int)
    ends = np.minimum(starts + np.rint(br.ti_s * rate).astype(int), len(raw))
    nxt = starts + np.rint(br.period_s * rate).astype(int)
    insp_start = np.full(n, -1)
    for i in range(n):
        a, m = int(starts[i]), int(ends[i])
        if m - a < MIN_INSP_SAMPLES:
            continue
        p = a + int(np.argmax(raw[a:m]))
        s, e = _bounds(raw, p, 0.0, m)
        x = raw[s:e]
        if len(x) < MIN_INSP_SAMPLES or x.max() < MIN_PEAK_LPM:
            continue
        q = float(x.max())
        valid[i], insp_start[i], qpeak[i] = True, s, q
        ned[i] = (q - x[len(x) // 2]) / q * 100.0
        fi[i] = float(x.mean()) / q
        gs, ge = _bounds(raw, p, GREY_UPPER_LPM, len(raw))
        x = raw[gs:ge] if ge - gs >= MIN_INSP_SAMPLES else x
        ln = len(x)
        mp = ln // 2  # upstream midpoint = start + round(len / 2)
        if ln > 12:
            left, right = x[:mp].sum(), x[mp + 1 :].sum()
            left_pct = 100.0 * left / (left + right) if left + right > 0 else 50.0
            top90 = 100.0 * np.count_nonzero(x > 0.9 * q) / ln
        else:
            left_pct, top90 = 50.0, 32.0
        g["skew"][i] = left_pct < 45 or left_pct > 55
        g["top_heavy"][i] = top90 > 40
        g["spike"][i] = top90 < 20
        mid = x[max(0, int(round(mp - 0.25 * ln))) : int(round(mp + 0.25 * ln))]
        g["flat_top"][i] = bool(len(mid)) and float(((mid - mid.mean()) ** 2).sum() / (0.5 * ln)) < 0.75
        g["multi_peak"][i] = _multi_peak(x)
    # cycle-based components: preceding expiration and the last five breaths
    for i in range(n):
        if not valid[i]:
            continue
        if i > 0 and nxt[i - 1] == starts[i]:  # contiguous with the previous breath
            ex0, ex1 = int(ends[i - 1]), int(insp_start[i])
            exp = raw[ex0:ex1] if ex1 > ex0 else np.empty(0)
            if len(exp) and exp.min() < GREY_LOWER_LPM:
                j = ex0 + int(np.argmin(exp))
                k = int(round(rate))  # upstream extrapolates over 25 samples = 1 s
                if j + k < len(raw) and raw[j] < raw[j + k] < 0:
                    pre_rest = insp_start[i] - (j + round(k * raw[j] / (raw[j] - raw[j + k])))
                else:
                    pre_rest = -10
                g["no_pause"][i] = pre_rest != 0 and pre_rest < 0.4 * rate
            else:
                g["multi_breath"][i] = True
        if i >= 5:
            span = (starts[i] - starts[i - 5]) / rate
            g["inspir_rate"][i] = span > 0 and round(5 * 60 / span) > 20
            peaks = qpeak[i - 4 : i + 1]
            g["variable_amp"][i] = bool(np.isfinite(peaks).all()) and float(np.var(peaks)) > 4.0
    return BreathShapes(valid, qpeak, ned, fi, g)

def rera_runs(
    shapes: BreathShapes, ned_fl_pct: float = 20.0, sigh_ratio: float = 1.5
) -> list[tuple[int, int, dict[str, float]]]:
    """NED RERA candidates as ``(first_breath, sigh_breath, details)``; the run is
    ``[first_breath, sigh_breath)`` and ``sigh_breath`` is the recovery breath."""
    fl = shapes.flow_limited(ned_fl_pct)
    out: list[tuple[int, int, dict[str, float]]] = []
    n, i = len(fl), 0
    while i < n:
        if not fl[i]:
            i += 1
            continue
        j = i
        while j < n and fl[j]:
            j += 1
        if RERA_MIN_BREATHS <= j - i <= RERA_MAX_BREATHS and j < n and shapes.valid[j]:
            ratio = float(shapes.qpeak_lpm[j] / shapes.qpeak_lpm[i:j].mean())
            if ratio >= sigh_ratio:
                out.append((i, j, {"max_ned_pct": float(shapes.ned_pct[i:j].max()), "sigh_ratio": ratio}))
        i = j
    return out

def _ps_at(
    times: Sequence[datetime], pressure: Sequence[ChannelData], epap: Sequence[ChannelData]
) -> np.ndarray:
    """Pressure support (pressure − EPAP) at each time; NaN where either channel is missing."""
    out = np.full(len(times), np.nan)
    for p in pressure:
        e = next((x for x in epap if x.start == p.start and x.rate_hz == p.rate_hz), None)
        if e is None:
            continue
        ps = p.values[: len(e.values)].astype(np.float64) - e.values[: len(p.values)].astype(np.float64)
        for k, t in enumerate(times):
            i = int((t - p.start).total_seconds() * p.rate_hz)
            if 0 <= i < len(ps) and np.isnan(out[k]):
                out[k] = ps[i]
    return out

@dataclass
class FlowShape:
    """Flow-shape metrics of a window, the PS split and raw NED RERA candidates (unfiltered)."""

    metrics: dict[str, Metric]
    by_ps: dict[str, dict[str, Metric]]
    rera: list[dict[str, Any]] = field(default_factory=list)
    breaths: int = 0

def _m(value: float | None, unit: str, method: str) -> Metric:
    return Metric(None if value is None else float(value), unit, Provenance.DERIVED, method)

def _summary(
    ned: np.ndarray, fl: np.ndarray, flags: dict[str, np.ndarray], ned_fl_pct: float
) -> dict[str, Metric]:
    n = len(ned)
    if not n:
        return {}
    out = {
        "ned_mean_pct": _m(float(np.mean(ned)), "%", "mean per-breath NED = (Qpeak - Q at 50 % Ti) / Qpeak"),
        "ned_flow_limited_pct": _m(
            100 * float(np.mean(fl)), "%", f"breaths with NED > {ned_fl_pct:g} % or flatness >= {FI_FL:g}"
        ),
        "glasgow_index": _m(
            float(sum(np.mean(flags[k]) for k in GLASGOW)), "",
            "Glasgow Index 0-9: sum of the shares of breaths flagged by each of 9 shape components",
        ),
    }  # fmt: skip
    for k in GLASGOW_REPORTED:
        out[f"glasgow_{k}_pct"] = _m(100 * float(np.mean(flags[k])), "%", f"breaths flagged '{k}' (Glasgow)")
    return out

def flow_shape(
    flow: Sequence[ChannelData], pressure: Sequence[ChannelData], epap: Sequence[ChannelData],
    thresholds: Mapping[str, float] | None = None, segmented: Sequence[Breaths] | None = None,
) -> FlowShape:  # fmt: skip
    """Flow-shape metrics over all breaths of ``flow`` with the PS = 0 / PS > 0 split.

    ``segmented`` = breaths already segmented from each part of ``flow`` (avoids a second pass)."""
    t = {**DEFAULTS, **{k: float(v) for k, v in (thresholds or {}).items() if k in DEFAULTS}}
    ned_l: list[np.ndarray] = []
    fl_l: list[np.ndarray] = []
    ps_l: list[np.ndarray] = []
    flags_l: dict[str, list[np.ndarray]] = {k: [] for k in GLASGOW}
    rera: list[dict[str, Any]] = []
    for n, part in enumerate(flow):
        br = segmented[n] if segmented is not None else segment_breaths(part)
        sh = breath_shapes(part, br)
        v = sh.valid
        ned_l.append(sh.ned_pct[v])
        fl_l.append(sh.flow_limited(t["ned_fl_pct"])[v])
        for k in GLASGOW:
            flags_l[k].append(sh.glasgow[k][v])
        mids = [br.at(s + ti / 2) for s, ti in zip(br.start_s[v], br.ti_s[v], strict=True)]
        ps_l.append(_ps_at(mids, pressure, epap))
        for i, j, det in rera_runs(sh, t["ned_fl_pct"], t["ned_rera_sigh_ratio"]):
            start, end = br.at(br.start_s[i]), br.at(br.start_s[j])
            rera.append({"start": start, "end": end, "duration_s": (end - start).total_seconds(),
                         "breaths": j - i, **det})  # fmt: skip
    ned = np.concatenate(ned_l) if ned_l else np.empty(0)
    fl = np.concatenate(fl_l) if fl_l else np.empty(0, bool)
    ps = np.concatenate(ps_l) if ps_l else np.empty(0)
    flags = {k: np.concatenate(flags_l[k]) if flags_l[k] else np.empty(0, bool) for k in GLASGOW}
    metrics = _summary(ned, fl, flags, t["ned_fl_pct"])
    by_ps: dict[str, dict[str, Metric]] = {}
    known = np.isfinite(ps)
    if len(ned) and known.any():
        on = known & (ps >= PS_ON_CMH2O)
        metrics["flow_shape_ps_pct"] = _m(
            100 * float(on.sum() / known.sum()), "%",
            f"breaths with pressure support (pressure - EPAP >= {PS_ON_CMH2O:g} cmH2O)",
        )  # fmt: skip
        for name, sel in (("ps_0", known & ~on), ("ps_gt_0", on)):
            part = _summary(ned[sel], fl[sel], {k: f[sel] for k, f in flags.items()}, t["ned_fl_pct"])
            if part:
                part["breaths"] = _m(float(sel.sum()), "", "breaths in this group")
            by_ps[name] = part
    return FlowShape(metrics, by_ps, rera, len(ned))
