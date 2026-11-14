"""Loop implementations of v0.1.7, kept as the reference for the vectorised code (#36).

Copied unchanged from ``flowshape.breath_shapes`` / ``oximetry._detect`` of v0.1.7; tests assert
that the vectorised versions return exactly the same results. Not imported by the package."""

from __future__ import annotations

import numpy as np

from pap_analytics.analytics.breathing import Breaths
from pap_analytics.analytics.flowshape import (
    GLASGOW,
    GREY_LOWER_LPM,
    GREY_UPPER_LPM,
    MIN_INSP_SAMPLES,
    MIN_PEAK_LPM,
    BreathShapes,
)
from pap_analytics.model import ChannelData


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

def breath_shapes_loop(flow: ChannelData, br: Breaths) -> BreathShapes:
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


def detect_loop(
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


