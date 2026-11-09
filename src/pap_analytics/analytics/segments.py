"""Statistics and (downsampled) signals for arbitrary time windows."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np

from pap_analytics.analytics.events import event_dict
from pap_analytics.analytics.results import AnalyticsError, SegmentStats, SeriesResult
from pap_analytics.analytics.stats import seconds_above, stat_block
from pap_analytics.analytics.timeparse import Context
from pap_analytics.model import CHANNELS, ChannelData, Metric, Provenance
from pap_analytics.store import Store

STAT_CHANNELS = (
    "pressure", "mask_pressure", "leak", "resp_rate", "tidal_volume", "minute_vent", "target_vent",
    "flow_limitation", "snore",
)  # fmt: skip
HIGH_RATE = {"flow", "pressure_hr"}
HIGH_RATE_MAX_MIN = 30
MAX_POINTS = 2000


def segment_stats(
    store: Store, device_id: str, start: datetime, end: datetime, ctx: Context | None = None,
    channels: Sequence[str] | None = None,
) -> SegmentStats:  # fmt: skip
    ctx = ctx or Context()
    if end <= start:
        raise AnalyticsError("segment end must be after start")
    names = list(channels) if channels else list(STAT_CHANNELS)
    bad = [c for c in names if c not in CHANNELS]
    if bad:
        raise AnalyticsError(f"unknown channels {bad}; known: {sorted(CHANNELS)}")
    out, notes = {}, []
    seconds = (end - start).total_seconds()
    for name in names:
        parts = store.channel_window(device_id, name, start, end)
        block = stat_block(parts, CHANNELS[name], expected=seconds * parts[0].rate_hz if parts else None)
        if block is None:
            notes.append(f"no {name} data in this window")
        else:
            out[name] = block
    leak = store.channel_window(device_id, "leak", start, end)
    large = None
    if leak:
        large = Metric(seconds_above(leak, ctx.large_leak_lps) / 60, "min", Provenance.DERIVED,
                       f"leak > {ctx.large_leak_lps} L/s")  # fmt: skip
    events = [event_dict(e) for e in store.events(device_id, start, end) if start <= e.start < end]
    if not out:
        notes.append("no channel data: the mask was off or the window is outside the ingested range")
    return SegmentStats(start, end, out, events, notes, large)


def downsample(parts: ChannelData | Sequence[ChannelData], max_points: int = 300) -> SeriesResult:
    """Equal-time buckets with min/mean/max (NaN-aware); empty buckets are None.

    Buckets start at the first sample; with few samples the native sampling interval is kept."""
    plist = [parts] if isinstance(parts, ChannelData) else list(parts)
    if not plist:
        raise AnalyticsError("no data to downsample")
    max_points = max(1, min(int(max_points), MAX_POINTS))
    first = plist[0]
    t0 = first.start
    span = max((p.start - t0).total_seconds() + len(p.values) / p.rate_hz for p in plist)
    period = 1.0 / first.rate_hz
    n_native = max(1, round(span / period))
    if n_native <= max_points:
        bucket_s, n = period, n_native
    else:
        n, bucket_s = max_points, span / max_points
    lo = np.full(n, np.inf)
    hi = np.full(n, -np.inf)
    sm = np.zeros(n)
    cnt = np.zeros(n, dtype=np.int64)
    for p in plist:
        v = p.values.astype(np.float64)
        off = (p.start - t0).total_seconds()
        idx = np.minimum(((off + np.arange(len(v)) / p.rate_hz) / bucket_s + 1e-9).astype(np.int64), n - 1)
        ok = np.isfinite(v)
        idx, v = idx[ok], v[ok]
        np.minimum.at(lo, idx, v)
        np.maximum.at(hi, idx, v)
        np.add.at(sm, idx, v)
        np.add.at(cnt, idx, 1)
    has = cnt > 0
    mean = np.where(has, sm / np.maximum(cnt, 1), np.nan)

    def lst(a: np.ndarray) -> list[float | None]:
        return [float(x) if m else None for x, m in zip(a, has, strict=True)]

    return SeriesResult(
        first.name, first.unit, t0, bucket_s, [round(i * bucket_s, 3) for i in range(n)],
        lst(lo), lst(mean), lst(hi),
    )  # fmt: skip


def get_signal(
    store: Store, device_id: str, start: datetime, end: datetime, channel: str, max_points: int = 300
) -> SeriesResult:
    if channel not in CHANNELS:
        raise AnalyticsError(f"unknown channel {channel!r}; known: {sorted(CHANNELS)}")
    if end <= start:
        raise AnalyticsError("end must be after start")
    if channel in HIGH_RATE and end - start > timedelta(minutes=HIGH_RATE_MAX_MIN):
        minutes = math.ceil((end - start).total_seconds() / 60)
        raise AnalyticsError(
            f"{channel} is a high-rate channel: windows are limited to {HIGH_RATE_MAX_MIN} min "
            f"(asked for {minutes} min). Use a shorter window, e.g. around one event."
        )
    notes = []
    if max_points > MAX_POINTS:
        notes.append(f"max_points capped to {MAX_POINTS}")
    parts = store.channel_window(device_id, channel, start, end)
    if not parts:
        raise AnalyticsError(f"no {channel} data between the given times")
    res = downsample(parts, max_points)
    res.notes = notes
    return res
