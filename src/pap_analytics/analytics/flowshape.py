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

def _segments(starts: np.ndarray, ends: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Concatenated sample indices of the ranges ``[starts[i], ends[i])``, their segment id and
    position within the segment (empty ranges contribute nothing)."""
    lens = np.maximum(ends - starts, 0)
    seg = np.repeat(np.arange(len(lens)), lens)
    first = np.cumsum(lens) - lens
    pos = np.arange(int(lens.sum())) - np.repeat(first, lens)
    return starts[seg] + pos, seg, pos

def _seg_sum(seg: np.ndarray, values: np.ndarray, n: int) -> np.ndarray:
    return np.bincount(seg, weights=values, minlength=n)

def _seg_max(seg: np.ndarray, values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(n, -np.inf)
    np.maximum.at(out, seg, values)
    return out

def _run_bounds(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For every index: start and end (exclusive) of the run of True values containing it."""
    n = len(mask)
    idx = np.arange(n)
    start = np.where(mask, -1, idx)  # index of the last False at or before i
    np.maximum.accumulate(start, out=start)
    end = np.where(mask, n, idx)[::-1]  # index of the first False at or after i
    np.minimum.accumulate(end, out=end)
    return start + 1, end[::-1]

def _multi_peak(x: np.ndarray, seg: np.ndarray, pos: np.ndarray, n: int, bump: float = 1.0) -> np.ndarray:
    """Upstream state machine per segment, vectorised: after the first peak (first sample below the
    running max, which starts at 0), a dip > ``bump`` below that peak, then a rise > ``bump`` above
    the lowest value since the dip started."""
    out = np.zeros(n, bool)
    if not len(x):
        return out
    big = 2.0 * (float(np.abs(x).max()) + bump + 1.0)
    off = seg * big
    run_max = np.maximum.accumulate(np.maximum(x, 0.0) + off) - off  # max(0, x[:k+1]) per segment
    prev_max = np.where(pos == 0, 0.0, np.concatenate(([0.0], run_max[:-1])))
    first_at = np.full(n, np.iinfo(np.int64).max)
    is_first = x < prev_max
    np.minimum.at(first_at, seg[is_first], pos[is_first])
    peak = np.zeros(n)
    has = first_at < np.iinfo(np.int64).max
    # running max frozen at the first decrease = max(0, x[:first]) = prev_max at that sample
    peak[seg[is_first & (pos == first_at[seg])]] = prev_max[is_first & (pos == first_at[seg])]
    after = has[seg] & (pos > first_at[seg])
    dip = after & (peak[seg] - x > bump)
    dip_at = np.full(n, np.iinfo(np.int64).max)
    np.minimum.at(dip_at, seg[dip], pos[dip])
    looking = pos >= dip_at[seg]
    # lowest value since the dip started, excluding the current sample
    y = np.where(looking, x, np.inf)
    run_min = np.minimum.accumulate(y - off) + off
    prev_min = np.where(pos == 0, np.inf, np.concatenate(([np.inf], run_min[:-1])))
    hit = looking & (pos > dip_at[seg]) & (x > prev_min + bump)
    out[np.unique(seg[hit])] = True
    return out

def breath_shapes(flow: ChannelData, br: Breaths) -> BreathShapes:
    """Shape metrics of every breath in ``br`` (segmented from ``flow``); see module docstring."""
    rate = flow.rate_hz
    raw = np.nan_to_num(flow.values.astype(np.float64)) * 60.0  # L/min, upstream thresholds
    n = len(br.start_s)
    qpeak, ned, fi = (np.full(n, np.nan) for _ in range(3))
    g = {name: np.zeros(n, bool) for name in GLASGOW}
    if n == 0 or not len(raw):
        return BreathShapes(np.zeros(n, bool), qpeak, ned, fi, g)
    starts = np.rint(br.start_s * rate).astype(np.int64)
    ends = np.minimum(starts + np.rint(br.ti_s * rate).astype(np.int64), len(raw))
    nxt = starts + np.rint(br.period_s * rate).astype(np.int64)
    ok = ends - starts >= MIN_INSP_SAMPLES
    # inspiratory peak of each segmented breath (first maximum, as argmax)
    idx, seg, pos = _segments(np.where(ok, starts, 0), np.where(ok, ends, 0))
    vals = raw[idx]
    smax = _seg_max(seg, vals, n)
    at_max = vals == smax[seg]
    p = np.full(n, -1, np.int64)
    first_pos = np.full(n, np.iinfo(np.int64).max)
    np.minimum.at(first_pos, seg[at_max], pos[at_max])
    p[ok] = starts[ok] + first_pos[ok]
    pp = np.where(ok, p, 0)
    # NED inspiration: run of raw > 0 around the peak, end capped at the breath's inspiration end
    rs0, re0 = _run_bounds(raw > 0.0)
    pos_peak = raw[pp] > 0.0
    s = np.where(pos_peak, rs0[pp], pp)
    e = np.where(pos_peak, np.minimum(re0[pp], np.maximum(ends, pp)), pp)
    idx, seg, pos = _segments(np.where(ok, s, 0), np.where(ok, e, 0))
    x = raw[idx]
    ln = np.where(ok, e - s, 0)
    q = _seg_max(seg, x, n)
    valid = ok & (ln >= MIN_INSP_SAMPLES) & (q >= MIN_PEAK_LPM)
    qpeak[valid] = q[valid]
    mid_val = raw[np.where(valid, s + ln // 2, 0)]
    ned[valid] = (q[valid] - mid_val[valid]) / q[valid] * 100.0
    fi[valid] = _seg_sum(seg, x, n)[valid] / ln[valid] / q[valid]
    # Glasgow inspiration: run of raw > grey zone around the peak, else the NED inspiration
    rs5, re5 = _run_bounds(raw > GREY_UPPER_LPM)
    above = raw[pp] > GREY_UPPER_LPM
    gs = np.where(above, rs5[pp], pp)
    ge = np.where(above, re5[pp], pp)
    use_g = ge - gs >= MIN_INSP_SAMPLES
    gs, ge = np.where(use_g, gs, s), np.where(use_g, ge, e)
    gs, ge = np.where(valid, gs, 0), np.where(valid, ge, 0)
    idx, seg, pos = _segments(gs, ge)
    x = raw[idx]
    gl = ge - gs
    mp = gl // 2  # upstream midpoint = start + round(len / 2)
    left = _seg_sum(seg, np.where(pos < mp[seg], x, 0.0), n)
    right = _seg_sum(seg, np.where(pos > mp[seg], x, 0.0), n)
    top = _seg_sum(seg, (x > 0.9 * np.nan_to_num(qpeak)[seg]).astype(np.float64), n)
    long = gl > 12
    tot = left + right
    with np.errstate(invalid="ignore", divide="ignore"):
        left_pct = np.where(long, np.where(tot > 0, 100.0 * left / tot, 50.0), 50.0)
        top90 = np.where(long, 100.0 * top / gl, 32.0)
    g["skew"] = valid & ((left_pct < 45) | (left_pct > 55))
    g["top_heavy"] = valid & (top90 > 40)
    g["spike"] = valid & (top90 < 20)
    q0 = np.maximum(0, np.round(mp - 0.25 * gl).astype(np.int64))
    q1 = np.round(mp + 0.25 * gl).astype(np.int64)
    in_mid = (pos >= q0[seg]) & (pos < q1[seg])
    cnt = np.bincount(seg[in_mid], minlength=n)
    msum = _seg_sum(seg[in_mid], x[in_mid], n)
    with np.errstate(invalid="ignore", divide="ignore"):
        mmean = msum / cnt
        dev = _seg_sum(seg[in_mid], (x[in_mid] - mmean[seg[in_mid]]) ** 2, n)
        g["flat_top"] = valid & (cnt > 0) & (dev / (0.5 * gl) < 0.75)
    g["multi_peak"] = valid & _multi_peak(x, seg, pos, n)
    # cycle-based components: preceding expiration and the last five breaths
    prev = np.zeros(n, bool)
    prev[1:] = valid[1:] & (nxt[:-1] == starts[1:])
    ex0 = np.zeros(n, np.int64)
    ex0[1:] = ends[:-1]
    ex1 = np.where(prev, s, 0)
    ex0 = np.where(prev & (ex1 > ex0), ex0, 0)
    ex1 = np.where(prev & (ex1 > ex0), ex1, 0)
    idx, seg, pos = _segments(ex0, ex1)
    x = raw[idx]
    emin = np.full(n, np.inf)
    np.minimum.at(emin, seg, x)
    at_min = x == emin[seg]
    jpos = np.full(n, np.iinfo(np.int64).max)
    np.minimum.at(jpos, seg[at_min], pos[at_min])
    deep = prev & (ex1 > ex0) & (emin < GREY_LOWER_LPM)
    g["multi_breath"] = prev & ~deep
    k = int(round(rate))  # upstream extrapolates over 25 samples = 1 s
    j = np.where(deep, ex0 + np.where(deep, jpos, 0), 0)
    inside = deep & (j + k < len(raw))
    rj = raw[j]
    rk = raw[np.where(inside, j + k, 0)]
    extrap = inside & (rj < rk) & (rk < 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        cross = j + np.round(k * rj / (rj - rk))
    pre_rest = np.where(extrap, s - cross, -10.0)
    g["no_pause"] = deep & (pre_rest != 0) & (pre_rest < 0.4 * rate)
    if n > 5:
        span = np.zeros(n)
        span[5:] = (starts[5:] - starts[:-5]) / rate
        with np.errstate(invalid="ignore", divide="ignore"):
            rate_ok = (span > 0) & (np.round(np.where(span > 0, 5 * 60 / span, 0.0)) > 20)
        g["inspir_rate"] = valid & rate_ok & (np.arange(n) >= 5)
        win = np.lib.stride_tricks.sliding_window_view(qpeak, 5)  # win[i - 4] = qpeak[i-4 : i+1]
        var = np.full(n, np.nan)
        fin = np.isfinite(win).all(axis=1)
        var[4:][fin] = win[fin].var(axis=1)
        g["variable_amp"] = valid & (np.arange(n) >= 5) & (var > 4.0)
    else:
        g["inspir_rate"] = np.zeros(n, bool)
        g["variable_amp"] = np.zeros(n, bool)
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
