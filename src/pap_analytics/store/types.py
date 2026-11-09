"""Store-facing value types (part of the store contract)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


class SchemaMismatchError(RuntimeError):
    """The store was written with a different ``SCHEMA_VERSION``."""


class StoreLockedError(RuntimeError):
    """Another writer holds the store lock."""


class StoreReadOnlyError(RuntimeError):
    """A write was attempted on a read-only store."""


@dataclass(frozen=True)
class SessionInfo:
    """A session without sample arrays; ``channels`` lists the stored canonical channel names."""

    id: str
    device_id: str
    therapy_day: date
    start: datetime
    end: datetime
    complete: bool
    fingerprint: str
    source_files: tuple[str, ...]
    channels: tuple[str, ...]


@dataclass(frozen=True)
class IngestLogEntry:
    started: datetime
    finished: datetime
    source: str
    device_id: str | None
    sessions_new: int = 0
    sessions_updated: int = 0
    sessions_unchanged: int = 0
    warnings: tuple[str, ...] = ()
    error: str | None = None
