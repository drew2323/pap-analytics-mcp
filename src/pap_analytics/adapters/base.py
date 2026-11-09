"""Adapter protocol and registry: vendor files → canonical model.

Adapters never import store or analytics. Auto-detection tries registered adapters in
registration order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from pap_analytics.model import Device, Session, Settings, TherapyDay


class UnknownSourceError(Exception):
    """No registered adapter recognises the source directory."""


@dataclass(frozen=True)
class SessionRef:
    id: str
    therapy_day: date
    start: datetime
    fingerprint: str
    files: tuple[Path, ...]


@runtime_checkable
class Adapter(Protocol):
    name: str
    warnings: list[str]  # unmapped channels/events, recoverable issues

    def detect(self, path: Path) -> bool: ...
    def device(self, path: Path) -> Device: ...
    def scan(self, path: Path, tz: ZoneInfo, boundary_hour: int) -> list[SessionRef]: ...
    def load_session(self, path: Path, ref: SessionRef, tz: ZoneInfo, boundary_hour: int) -> Session: ...
    def therapy_days(self, path: Path, tz: ZoneInfo) -> list[TherapyDay]: ...
    def settings(self, path: Path, tz: ZoneInfo) -> list[Settings]: ...


_REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    """Register an adapter (replaces an existing one of the same name)."""
    _REGISTRY[adapter.name] = adapter


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def registered() -> list[str]:
    return list(_REGISTRY)


def get(name: str) -> Adapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownSourceError(f"unknown adapter {name!r}; registered: {registered()}") from None


def detect(path: Path) -> Adapter:
    """Return the first registered adapter that recognises ``path``."""
    for adapter in _REGISTRY.values():
        if adapter.detect(path):
            return adapter
    raise UnknownSourceError(f"no adapter recognises {path}; tried: {', '.join(registered()) or '(none)'}")
