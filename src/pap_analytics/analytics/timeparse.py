"""Resolve user input (days, clock times, ISO datetimes) and carry analysis context.

A therapy day runs boundary-hour to boundary-hour local time, so ``02:10`` on day 2026-11-08
means 2026-11-09 02:10. All returned datetimes are timezone-aware UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pap_analytics.analytics.results import AnalyticsError
from pap_analytics.config import Config
from pap_analytics.model import therapy_day
from pap_analytics.store import Store


@dataclass(frozen=True)
class Context:
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Prague"))
    boundary_hour: int = 12
    large_leak_lps: float = 0.4  # ≈ 24 L/min

    @classmethod
    def from_config(cls, config: Config) -> Context:
        tz = ZoneInfo(config.sources[0].timezone) if config.sources else ZoneInfo("Europe/Prague")
        return cls(tz, config.day_boundary_hour, float(config.thresholds.get("large_leak_lps", 0.4)))


def day_window(day: date, ctx: Context) -> tuple[datetime, datetime]:
    """UTC start/end of the therapy day (boundary → boundary, local)."""
    start = datetime.combine(day, time(ctx.boundary_hour), tzinfo=ctx.tz)
    end = datetime.combine(day + timedelta(days=1), time(ctx.boundary_hour), tzinfo=ctx.tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def latest_day(store: Store, device_id: str) -> date:
    days = [s.therapy_day for s in store.sessions(device_id)]
    days += [d.date for d in store.therapy_days(device_id, date(1900, 1, 1), date(9999, 12, 31))]
    if not days:
        raise AnalyticsError(f"no data for device {device_id}; run `pap ingest` first")
    return max(days)


def parse_day(
    value: str | date, ctx: Context, store: Store | None = None, device_id: str | None = None,
    now: datetime | None = None,
) -> date:  # fmt: skip
    """``YYYY-MM-DD``, ``last`` (latest day in the store) or ``yesterday`` (local calendar yesterday)."""
    if isinstance(value, date):
        return value
    v = value.strip().lower()
    if v == "last":
        if store is None or device_id is None:
            raise AnalyticsError("'last' needs a store and device")
        return latest_day(store, device_id)
    if v == "yesterday":
        return (now or datetime.now(UTC)).astimezone(ctx.tz).date() - timedelta(days=1)
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise AnalyticsError(f"bad day {value!r}: use YYYY-MM-DD, 'last' or 'yesterday'") from None


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_time(value: str | datetime, day: date, ctx: Context) -> datetime:
    """``HH:MM[:SS]`` inside the therapy day of ``day``, or an ISO datetime (naive = local)."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=ctx.tz)
        return dt.astimezone(UTC)
    m = _HHMM.match(value.strip())
    if m:
        h, mi, s = int(m[1]), int(m[2]), int(m[3] or 0)
        if h > 23 or mi > 59 or s > 59:
            raise AnalyticsError(f"bad time {value!r}")
        d = day if h >= ctx.boundary_hour else day + timedelta(days=1)
        return datetime.combine(d, time(h, mi, s), tzinfo=ctx.tz).astimezone(UTC)
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise AnalyticsError(f"bad time {value!r}: use HH:MM or an ISO datetime") from None
    return parse_time(dt, day, ctx)


def day_of(ts: datetime, ctx: Context) -> date:
    return therapy_day(ts, ctx.tz, ctx.boundary_hour)


def resolve_device(store: Store, device_id: str | None) -> str:
    devices = store.devices()
    if device_id:
        if device_id not in {d.id for d in devices}:
            raise AnalyticsError(f"unknown device {device_id!r}; known: {[d.id for d in devices]}")
        return device_id
    if len(devices) == 1:
        return devices[0].id
    if not devices:
        raise AnalyticsError("store is empty; run `pap ingest` first")
    raise AnalyticsError(f"several devices, pass device_id: {[d.id for d in devices]}")
