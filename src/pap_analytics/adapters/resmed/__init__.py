"""ResMed AirSense/AirCurve 10/11 SD-card adapter (also the AirCANnect share layout).

Reads ``Identification.*``, ``STR.edf`` and ``DATALOG`` (BRP/PLD/SA2/EVE/CSL) and returns
canonical objects. Read-only; knows nothing about storage or analytics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pap_analytics.adapters import base
from pap_analytics.adapters.base import SessionRef
from pap_analytics.adapters.resmed import datalog, identification, str_summary
from pap_analytics.adapters.resmed.detect import find, is_resmed
from pap_analytics.model import Device, Session, Settings, TherapyDay

GROWING_WINDOW = timedelta(minutes=15)


class ResmedAdapter:
    name = "resmed"

    def __init__(self, growing_window: timedelta = GROWING_WINDOW, clock=None) -> None:  # noqa: ANN001
        self.warnings: list[str] = []
        self.growing_window = growing_window
        self._clock = clock or (lambda: datetime.now(UTC))

    def detect(self, path: Path) -> bool:
        return is_resmed(Path(path))

    def device(self, path: Path) -> Device:
        return identification.read_device(Path(path))

    def scan(self, path: Path, tz: ZoneInfo, boundary_hour: int) -> list[SessionRef]:
        return datalog.scan(Path(path), self.device(path).id, tz, boundary_hour)

    def load_session(self, path: Path, ref: SessionRef, tz: ZoneInfo, boundary_hour: int) -> Session:
        path = Path(path)
        groups = datalog.list_groups(path)
        newest = bool(groups) and ref.start == max(datalog.prefix_start(p, tz) for p in groups)
        return datalog.load_session(
            path,
            ref,
            self.device(path).id,
            tz,
            boundary_hour,
            newest=newest,
            growing_window=self.growing_window,
            now=self._clock(),
            warnings=self.warnings,
        )

    def therapy_days(self, path: Path, tz: ZoneInfo) -> list[TherapyDay]:
        str_path = find(Path(path), "STR.edf")
        assert str_path is not None
        return str_summary.read_therapy_days(str_path, self.device(path).id, tz)

    def settings(self, path: Path, tz: ZoneInfo) -> list[Settings]:
        str_path = find(Path(path), "STR.edf")
        assert str_path is not None
        return str_summary.read_settings(str_path, self.device(path).id)


base.register(ResmedAdapter())

__all__ = ["GROWING_WINDOW", "ResmedAdapter"]
