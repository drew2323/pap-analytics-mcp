"""Optional PNG rendering of a segment (extra ``plot``: matplotlib). Boundaries: read-only on the store.

One panel per requested channel (SpO2 and pulse share a secondary axis when present),
device events as shaded vertical bands. Output is a small PNG (<= ``MAX_BYTES``).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime

from pap_analytics.analytics.results import AnalyticsError
from pap_analytics.analytics.segments import HIGH_RATE, HIGH_RATE_MAX_MIN, downsample
from pap_analytics.analytics.timeparse import Context
from pap_analytics.model import CHANNELS, EventType
from pap_analytics.store import Store

MAX_BYTES = 200_000
DEFAULT_CHANNELS = ("pressure", "leak", "minute_vent", "spo2")
SECONDARY = {"spo2"}
COLORS = {
    EventType.HYPOPNEA: "#f4a261", EventType.OBSTRUCTIVE_APNEA: "#e63946", EventType.CENTRAL_APNEA: "#9b5de5",
    EventType.APNEA: "#e63946",
}  # fmt: skip


def available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


def plot_segment(
    store: Store, device_id: str, start: datetime, end: datetime,
    channels: Sequence[str] | None = None, ctx: Context | None = None,
) -> bytes:  # fmt: skip
    """PNG of ``channels`` (default pressure, leak, minute_vent, spo2) between ``start`` and ``end``."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise AnalyticsError(
            "plotting needs the 'plot' extra: pip install 'pap-analytics-mcp[plot]'"
        ) from exc
    from datetime import timedelta

    ctx = ctx or Context()
    if end <= start:
        raise AnalyticsError("segment end must be after start")
    names = list(channels) if channels else list(DEFAULT_CHANNELS)
    bad = [c for c in names if c not in CHANNELS]
    if bad:
        raise AnalyticsError(f"unknown channels {bad}; known: {sorted(CHANNELS)}")
    if (end - start) > timedelta(minutes=HIGH_RATE_MAX_MIN) and set(names) & HIGH_RATE:
        raise AnalyticsError(
            f"flow/pressure_hr can only be plotted for windows up to {HIGH_RATE_MAX_MIN} min"
        )
    series = {}
    for name in names:
        parts = store.channel_window(device_id, name, start, end)
        if parts:
            series[name] = downsample(parts, 500)
    if not series:
        raise AnalyticsError("no channel data in this window")
    events = [e for e in store.events(device_id, start, end) if e.type in COLORS]

    primary = [n for n in series if n not in SECONDARY]
    rows = max(1, len(primary))
    fig, axes = plt.subplots(rows, 1, figsize=(9, 1.9 * rows + 0.6), sharex=True, squeeze=False, dpi=80)
    axes = [a for a in axes[:, 0]]
    tz = ctx.tz

    def times(s):  # type: ignore[no-untyped-def]
        return [(s.start + timedelta(seconds=x)).astimezone(tz) for x in s.t]

    def draw(ax, name):  # type: ignore[no-untyped-def]
        s = series[name]
        x = times(s)
        mean = [float("nan") if v is None else v for v in s.mean]
        lo = [float("nan") if v is None else v for v in s.min]
        hi = [float("nan") if v is None else v for v in s.max]
        ax.fill_between(x, lo, hi, alpha=0.25, linewidth=0)
        ax.plot(x, mean, linewidth=1)
        ax.set_ylabel(f"{name}\n[{s.unit}]", fontsize=8)

    for ax, name in zip(axes, primary or ["spo2"], strict=False):
        if primary:
            draw(ax, name)
    if "spo2" in series:
        host = axes[0]
        sec = host.twinx() if primary else host
        draw(sec, "spo2")
        sec.set_ylim(80, 100)
        sec.yaxis.label.set_color("tab:red")
        sec.lines[-1].set_color("tab:red")
    for ax in axes:
        for e in events:
            a = e.start.astimezone(tz)
            ax.axvspan(a, a + timedelta(seconds=e.duration_s), color=COLORS[e.type], alpha=0.35, linewidth=0)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.2)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    axes[0].set_title(
        f"{start.astimezone(tz):%Y-%m-%d %H:%M}–{end.astimezone(tz):%H:%M}  "
        f"(bands: events; derived from device data, not a diagnosis)",
        fontsize=8,
    )
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    data = buf.getvalue()
    if len(data) > MAX_BYTES:
        raise AnalyticsError(
            f"plot is {len(data)} bytes (> {MAX_BYTES}); narrow the window or fewer channels"
        )
    return data
