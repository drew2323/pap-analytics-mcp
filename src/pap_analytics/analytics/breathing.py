"""Breath-by-breath analysis of the waveform-rate ``flow`` channel and breathing stability.

All results are ``derived``. Nothing here scores respiratory events (device events stay the AHI
reference, ADR-011) and nothing here is sleep staging: "calm" / "restless" describe the
regularity of breathing only.

* **Breath segmentation:** flow is smoothed (0.2 s moving mean); a breath runs from one
  inspiration start (flow rising above +``CROSS_LPS`` after having been below −``CROSS_LPS``) to
  the next. Inspiratory time / expiratory time must each be >= ``MIN_PHASE_S``; cycles longer than
  ``MAX_BREATH_S`` are pauses, not breaths. Tidal volume = integral of inspiratory flow.
* **Irregularity** of a 30 s epoch = mean of the coefficient of variation of tidal volume and of
  breath period over the ``IRREGULARITY_WINDOW_S`` window centred on the epoch (needs
  ``MIN_BREATHS`` breaths, else the epoch is ``unknown``).
* **State:** ``restless`` when irregularity >= ``restless_irregularity``, else ``calm``; a single
  epoch whose state differs from two equal classified neighbours is set to ``unknown`` (no guess).
  A *restless period* is a run of restless epochs lasting >= ``restless_min_s``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from pap_analytics.model import ChannelData

DEFAULTS: dict[str, float] = {
    "restless_irregularity": 0.3,
    "restless_min_s": 60.0,
}
CROSS_LPS = 0.03  # hysteresis around zero flow, L/s
SMOOTH_S = 0.2
MIN_PHASE_S = 0.3
MAX_BREATH_S = 20.0
EPOCH_S = 30.0
IRREGULARITY_WINDOW_S = 120.0
MIN_BREATHS = 15
CALM, RESTLESS, UNKNOWN = "calm", "restless", "unknown"


@dataclass
class Breaths:
    """Breaths of one flow recording; times in seconds from ``origin``."""

    origin: datetime
    start_s: np.ndarray
    ti_s: np.ndarray
    te_s: np.ndarray
    vt_ml: np.ndarray

    @property
    def period_s(self) -> np.ndarray:
        return self.ti_s + self.te_s

    def at(self, s: float) -> datetime:
        return self.origin + timedelta(seconds=float(s))


def segment_breaths(flow: ChannelData) -> Breaths:
    """Breaths in a flow recording (see module docstring); NaN flow counts as zero flow."""
    rate = flow.rate_hz
    v = np.nan_to_num(flow.values.astype(np.float64))
    k = max(1, int(round(SMOOTH_S * rate)))
    f = np.convolve(v, np.ones(k) / k, mode="same")
    mark = np.where(f > CROSS_LPS, 1, np.where(f < -CROSS_LPS, -1, 0))
    idx = np.where(mark != 0, np.arange(len(f)), 0)
    np.maximum.accumulate(idx, out=idx)
    state = mark[idx]  # last non-zero mark (hysteresis); 0 before the first one
    up = np.flatnonzero((state[1:] == 1) & (state[:-1] == -1)) + 1
    down = np.flatnonzero((state[1:] == -1) & (state[:-1] == 1)) + 1
    empty = np.empty(0)
    if len(up) < 2 or not len(down):
        return Breaths(flow.start, empty, empty, empty, empty)
    a, b = up[:-1], up[1:]
    j = np.searchsorted(down, a, side="right")
    ok = j < len(down)
    a, b, j = a[ok], b[ok], j[ok]
    m = down[j]
    ok = m < b
    a, b, m = a[ok], b[ok], m[ok]
    ti, te = (m - a) / rate, (b - m) / rate
    ok = (ti >= MIN_PHASE_S) & (te >= MIN_PHASE_S) & (ti + te <= MAX_BREATH_S)
    a, m, ti, te = a[ok], m[ok], ti[ok], te[ok]
    csum = np.concatenate(([0.0], np.cumsum(f)))
    vt = (csum[m] - csum[a]) / rate * 1000.0
    return Breaths(flow.start, a / rate, ti, te, vt)


def epoch_irregularity(br: Breaths, duration_s: float) -> np.ndarray:
    """Irregularity per 30 s epoch of a recording of ``duration_s`` (NaN = unknown)."""
    n = int(duration_s // EPOCH_S)
    out = np.full(n, np.nan)
    if not len(br.start_s) or n == 0:
        return out
    half = (IRREGULARITY_WINDOW_S - EPOCH_S) / 2
    lo = np.searchsorted(br.start_s, np.arange(n) * EPOCH_S - half)
    hi = np.searchsorted(br.start_s, np.arange(n) * EPOCH_S + EPOCH_S + half)
    vt, per = br.vt_ml, br.period_s
    for e in range(n):
        if hi[e] - lo[e] < MIN_BREATHS:
            continue
        w_vt, w_per = vt[lo[e] : hi[e]], per[lo[e] : hi[e]]
        if w_vt.mean() <= 0:
            continue
        out[e] = (w_vt.std() / w_vt.mean() + w_per.std() / w_per.mean()) / 2
    return out


def epoch_states(irregularity: np.ndarray, restless_irregularity: float) -> list[str]:
    """``calm`` / ``restless`` / ``unknown`` per epoch, isolated single flips set to ``unknown``."""
    raw = [
        UNKNOWN if not np.isfinite(x) else RESTLESS if x >= restless_irregularity else CALM
        for x in irregularity
    ]
    out = list(raw)
    for i in range(1, len(raw) - 1):
        if raw[i] != UNKNOWN and raw[i - 1] == raw[i + 1] != UNKNOWN and raw[i] != raw[i - 1]:
            out[i] = UNKNOWN
    return out


def runs(states: list[str]) -> list[tuple[int, int, str]]:
    """Run-length encoding ``(first_epoch, end_epoch_exclusive, state)``."""
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(states):
        j = i
        while j < len(states) and states[j] == states[i]:
            j += 1
        out.append((i, j, states[i]))
        i = j
    return out


def breath_ratio_before(br: Breaths, t_s: float, before_s: float = 30.0) -> float | None:
    """Smallest breath in the ``before_s`` before ``t_s`` / median breath of the baseline
    ``[t_s − 120, t_s − before_s)``; None when either window has too few breaths."""
    st = br.start_s
    base = br.vt_ml[(st >= t_s - 120.0) & (st < t_s - before_s)]
    pre = br.vt_ml[(st >= t_s - before_s) & (st < t_s)]
    if len(base) < 5 or len(pre) < 2 or np.median(base) <= 0:
        return None
    return float(pre.min() / np.median(base))
