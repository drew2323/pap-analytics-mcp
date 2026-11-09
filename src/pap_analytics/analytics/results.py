"""Result dataclasses of the analytics layer and their JSON form.

Results are plain data: every metric carries unit and provenance, ``notes`` explain gaps.
``to_dict`` produces stable key order (dataclass field order), rounds floats, maps NaN to
``None`` and renders datetimes as ISO strings in local time with offset. No I/O here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from pap_analytics.model import Metric


class AnalyticsError(ValueError):
    """User-correctable problem (bad window, unknown channel, ...); message says how to fix it."""


@dataclass
class StatBlock:
    unit: str
    min: float | None
    p5: float | None
    p50: float | None
    mean: float | None
    p95: float | None
    max: float | None
    coverage_pct: float
    provenance: str = "derived"


@dataclass
class NightRow:
    date: date
    usage_min: Metric
    sessions: int
    ahi_device: Metric | None
    ahi_derived: Metric
    events: dict[str, int]
    leak_p95: Metric | None
    pressure_p95: Metric | None
    has_oximetry: bool
    usage_min_derived: Metric | None = None  # sum of session durations, next to the device value


@dataclass
class NightSummary:
    date: date
    device_id: str
    sessions: list[dict[str, Any]]
    mask_off_gaps: list[dict[str, Any]]
    events_by_type: dict[str, int]
    events_per_hour: list[dict[str, Any]]
    device_metrics: dict[str, Metric]
    derived_metrics: dict[str, Metric]
    channels: dict[str, StatBlock]
    settings: dict[str, Any] | None
    oximetry: dict[str, Any] | None
    notes: list[str] = field(default_factory=list)


@dataclass
class SegmentStats:
    start: datetime
    end: datetime
    channels: dict[str, StatBlock]
    events: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)
    large_leak_min: Metric | None = None


@dataclass
class SeriesResult:
    channel: str
    unit: str
    start: datetime
    bucket_s: float
    t: list[float]
    min: list[float | None]
    mean: list[float | None]
    max: list[float | None]
    notes: list[str] = field(default_factory=list)


@dataclass
class EventsResult:
    start: datetime
    end: datetime
    total: int
    returned: int
    events: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


@dataclass
class EventCluster:
    start: datetime
    end: datetime
    count: int
    by_type: dict[str, int]


@dataclass
class Comparison:
    a_nights: int
    b_nights: int
    metrics: dict[str, dict[str, Any]]
    notes: list[str] = field(default_factory=list)


def to_dict(obj: Any, tz: ZoneInfo, digits: int = 3) -> Any:
    """Convert a result (dataclass tree) into JSON-serialisable primitives."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name), tz, digits) for f in fields(obj)}
    if isinstance(obj, datetime):
        return obj.astimezone(tz).isoformat(timespec="seconds")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else round(obj, digits)
    if isinstance(obj, dict):
        return {str(k): to_dict(v, tz, digits) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_dict(v, tz, digits) for v in obj]
    if hasattr(obj, "item") and hasattr(obj, "dtype"):  # numpy scalar
        return to_dict(obj.item(), tz, digits)
    return obj
