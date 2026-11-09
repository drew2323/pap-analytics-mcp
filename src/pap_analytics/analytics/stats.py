"""Numeric helpers shared by analytics modules (NaN-aware statistics over channel windows)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from pap_analytics.analytics.results import StatBlock
from pap_analytics.model import ChannelData


def concat(parts: Sequence[ChannelData]) -> np.ndarray:
    if not parts:
        return np.empty(0, dtype=np.float64)
    return np.concatenate([p.values for p in parts]).astype(np.float64)


def stat_block(parts: Sequence[ChannelData], unit: str, expected: float | None = None) -> StatBlock | None:
    """Statistics over the concatenated windows. ``coverage_pct`` = finite samples / expected
    (default: all samples present in ``parts``). None when there is no data at all."""
    if not parts:
        return None
    v = concat(parts)
    finite = v[np.isfinite(v)]
    total = float(expected) if expected is not None else float(len(v))
    cov = 0.0 if total <= 0 else min(100.0, 100.0 * len(finite) / total)
    if len(finite) == 0:
        return StatBlock(unit, None, None, None, None, None, None, cov)
    p5, p50, p95 = np.percentile(finite, [5, 50, 95])
    return StatBlock(
        unit, float(finite.min()), float(p5), float(p50), float(finite.mean()), float(p95),
        float(finite.max()), cov,
    )  # fmt: skip


def percentile(parts: Sequence[ChannelData], q: float) -> float | None:
    v = concat(parts)
    v = v[np.isfinite(v)]
    return float(np.percentile(v, q)) if len(v) else None


def seconds_above(parts: Sequence[ChannelData], threshold: float) -> float:
    total = 0.0
    for p in parts:
        v = p.values
        total += float(np.count_nonzero(np.isfinite(v) & (v > threshold))) / p.rate_hz
    return total


def has_finite(parts: Sequence[ChannelData]) -> bool:
    return any(bool(np.isfinite(p.values).any()) for p in parts)
