"""Canonical, vendor-neutral PAP data model (contract, see docs/ARCHITECTURE.md).

Pure data definitions and the therapy-day rule. No I/O, no vendor knowledge.
Any change to persisted shape must bump ``SCHEMA_VERSION``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

import numpy as np

SCHEMA_VERSION: int = 2

# Canonical channel name -> unit.
CHANNELS: dict[str, str] = {
    "flow": "L/s",
    "pressure": "cmH2O",
    "pressure_hr": "cmH2O",
    "mask_pressure": "cmH2O",
    "epap": "cmH2O",
    "leak": "L/s",
    "resp_rate": "1/min",
    "tidal_volume": "mL",
    "minute_vent": "L/min",
    "target_vent": "L/min",
    "ie_ratio": "",
    "ti": "s",
    "snore": "vendor",
    "flow_limitation": "0-1",
    "spo2": "%",
    "pulse": "1/min",
}


# Accepted input names -> canonical channel. `pressure_hr` is the 25 Hz mask pressure (ResMed
# `Press.40ms`); `mask_pressure` is the 0.5 Hz one (`MaskPress.2s`).
CHANNEL_ALIASES: dict[str, str] = {"mask_pressure_hr": "pressure_hr"}


class EventType(StrEnum):
    OBSTRUCTIVE_APNEA = "obstructive_apnea"
    CENTRAL_APNEA = "central_apnea"
    APNEA = "apnea"
    HYPOPNEA = "hypopnea"
    RERA = "rera"
    AROUSAL = "arousal"
    FLOW_LIMITATION = "flow_limitation"
    CSR = "csr"
    PERIODIC_BREATHING = "periodic_breathing"
    LARGE_LEAK = "large_leak"
    DESATURATION = "desaturation"
    RECORDING_START = "recording_start"
    OTHER = "other"


class Provenance(StrEnum):
    DEVICE = "device"
    DERIVED = "derived"


@dataclass(frozen=True)
class Metric:
    value: float | None
    unit: str
    provenance: Provenance
    method: str = ""


@dataclass(frozen=True)
class Device:
    id: str
    vendor: str
    model: str
    model_code: str | None
    serial: str | None
    firmware: str | None
    source_kind: str  # e.g. "resmed-sd"


@dataclass(frozen=True, eq=False)
class ChannelData:
    """Regularly sampled channel; sample i is at ``start + i / rate_hz``."""

    name: str
    unit: str
    rate_hz: float
    start: datetime
    values: np.ndarray  # float32, NaN = missing


@dataclass(frozen=True)
class Event:
    start: datetime
    duration_s: float
    type: EventType
    raw_type: str
    provenance: Provenance


@dataclass(frozen=True)
class Session:
    id: str
    device_id: str
    therapy_day: date
    start: datetime
    end: datetime
    complete: bool
    fingerprint: str
    source_files: tuple[str, ...]
    channels: tuple[ChannelData, ...]
    events: tuple[Event, ...]
    mode: str | None = None  # therapy mode this session ran in (from its recorded data), None = unknown


@dataclass(frozen=True)
class TherapyDay:
    device_id: str
    date: date
    usage_s: float | None
    mask_intervals: tuple[tuple[datetime, datetime], ...]
    metrics: dict[str, Metric]


@dataclass(frozen=True)
class Settings:
    device_id: str
    valid_from: date
    mode: str | None
    values: dict[str, float | str]
    raw: dict


def session_id(device_id: str, start: datetime) -> str:
    """Session id: ``<device_id>:<UTC start as YYYYmmddTHHMMSS>``."""
    if start.tzinfo is None:
        raise ValueError("session start must be timezone-aware")
    utc = start.astimezone(ZoneInfo("UTC"))
    return f"{device_id}:{utc:%Y%m%dT%H%M%S}"


def therapy_day(ts: datetime, tz: ZoneInfo, boundary_hour: int = 12) -> date:
    """Local date on which the ``boundary_hour``→``boundary_hour`` window containing ``ts`` starts."""
    if ts.tzinfo is None:
        raise ValueError("ts must be timezone-aware")
    local = ts.astimezone(tz)
    day = local.date()
    if local.hour < boundary_hour:
        day -= timedelta(days=1)
    return day
