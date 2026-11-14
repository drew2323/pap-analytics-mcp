"""Statistics and (downsampled) signals for arbitrary time windows."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from pap_analytics.analytics.events import event_dict
from pap_analytics.analytics.results import AnalyticsError, SegmentStats, SeriesResult, SignalsResult
from pap_analytics.analytics.stats import seconds_above, stat_block
from pap_analytics.analytics.timeparse import Context
from pap_analytics.model import CHANNEL_ALIASES, CHANNELS, ChannelData, Metric, Provenance
from pap_analytics.store import Store

STAT_CHANNELS = (
    "pressure", "mask_pressure", "leak", "resp_rate", "tidal_volume", "minute_vent", "target_vent",
    "flow_limitation", "snore",
)  # fmt: skip
HIGH_RATE = {"flow", "pressure_hr"}
HIGH_RATE_MAX_MIN = 30
MAX_POINTS = 2000
MAX_SIGNAL_ROWS = 2000  # get_signals grid rows (= 80 s at 25 Hz)
MAX_SIGNAL_CHANNELS = 8


def segment_stats(
    store: Store, device_id: str, start: datetime, end: datetime, ctx: Context | None = None,
    channels: Sequence[str] | None = None,
) -> SegmentStats:  # fmt: skip
    """Channel statistics and events of a window (one shared catalog connection)."""
    with store.connection():
        return _segment_stats(store, device_id, start, end, ctx or Context(), channels)

def _segment_stats(
    store: Store, device_id: str, start: datetime, end: datetime, ctx: Context,
    channels: Sequence[str] | None,
) -> SegmentStats:  # fmt: skip
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


def resolve_channel(name: str) -> str:
    """Canonical channel for an accepted name (resolves aliases)."""
    return CHANNEL_ALIASES.get(name, name)


def get_signal(
    store: Store, device_id: str, start: datetime, end: datetime, channel: str, max_points: int = 300
) -> SeriesResult:
    channel = resolve_channel(channel)
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


def get_signals(
    store: Store, device_id: str, start: datetime, end: datetime, channels: Sequence[str]
) -> SignalsResult:
    """Raw samples of several channels on one grid given by the finest channel (no aggregation)."""
    if end <= start:
        raise AnalyticsError("end must be after start")
    names = list(dict.fromkeys(resolve_channel(c) for c in channels))
    if not names:
        raise AnalyticsError("give at least one channel")
    if len(names) > MAX_SIGNAL_CHANNELS:
        raise AnalyticsError(f"at most {MAX_SIGNAL_CHANNELS} channels per call")
    bad = [c for c in names if c not in CHANNELS]
    if bad:
        raise AnalyticsError(f"unknown channels {bad}; known: {sorted(CHANNELS)}")
    notes: list[str] = []
    data: dict[str, list[ChannelData]] = {}
    for name in names:
        parts = store.channel_window(device_id, name, start, end)
        if parts:
            data[name] = list(parts)
        else:
            notes.append(f"no {name} data in this window")
    if not data:
        raise AnalyticsError("no data for any requested channel between the given times")
    fine = max(data, key=lambda n: data[n][0].rate_hz)  # parts are already sliced to the window
    step = 1.0 / data[fine][0].rate_hz
    t0 = min(p.start for p in data[fine])
    rows = max(round(((p.start - t0).total_seconds() / step) + len(p.values)) for p in data[fine])
    if rows > MAX_SIGNAL_ROWS:
        raise AnalyticsError(
            f"{fine} has {data[fine][0].rate_hz:g} Hz: raw samples are limited to {MAX_SIGNAL_ROWS} rows "
            f"= {MAX_SIGNAL_ROWS * step:g} s (asked for {rows * step:.0f} s). "
            "Use a shorter window, or only slow channels."
        )
    out: dict[str, dict[str, Any]] = {}
    for name, parts in data.items():
        vals: list[float | None] = [None] * rows
        off_grid = 0
        for p in parts:
            pos = ((p.start - t0).total_seconds() + np.arange(len(p.values)) / p.rate_hz) / step
            idx = np.rint(pos).astype(np.int64)
            off_grid += int(np.sum(np.abs(pos - idx) > 1e-3))
            for i, v in zip(idx.tolist(), p.values.tolist(), strict=True):
                if 0 <= i < rows and math.isfinite(v):
                    vals[i] = v
        if off_grid:
            notes.append(f"{name}: {off_grid} samples were off the grid and snapped to the nearest row")
        out[name] = {"unit": CHANNELS[name], "rate_hz": data[name][0].rate_hz, "values": vals}
    return SignalsResult(t0, step, rows, out, notes)
