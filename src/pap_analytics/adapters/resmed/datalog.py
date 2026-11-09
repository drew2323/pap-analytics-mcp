"""``DATALOG/<day>/<YYYYMMDD_HHMMSS>_<TYPE>.edf`` scanning and session loading."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from pap_analytics.adapters.base import SessionRef
from pap_analytics.adapters.resmed.detect import find
from pap_analytics.adapters.resmed.mapping import CHANNEL_MAP, SANITIZERS, map_event
from pap_analytics.edf import EdfFile
from pap_analytics.model import (
    ChannelData,
    Event,
    EventType,
    Provenance,
    Session,
    session_id,
    therapy_day,
)

_NAME = re.compile(r"^(\d{8})_(\d{6})_([A-Za-z0-9]+)\.edf$", re.I)
SIGNAL_TYPES = ("BRP", "PLD", "SA2")
EVENT_TYPES = ("EVE", "CSL")


def list_groups(path: Path) -> dict[str, dict[str, Path]]:
    """prefix ``YYYYMMDD_HHMMSS`` → {TYPE: file}. The folder name is deliberately not used."""
    groups: dict[str, dict[str, Path]] = {}
    root = find(path, "DATALOG")
    if root is None:
        return groups
    for day in sorted(root.iterdir()):
        if not day.is_dir():
            continue
        for f in sorted(day.iterdir()):
            m = _NAME.match(f.name)
            if m:
                groups.setdefault(f"{m.group(1)}_{m.group(2)}", {})[m.group(3).upper()] = f
    return groups


def fingerprint(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.name):
        st = f.stat()
        h.update(f"{f.name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def prefix_start(prefix: str, tz: ZoneInfo) -> datetime:
    return datetime.strptime(prefix, "%Y%m%d_%H%M%S").replace(tzinfo=tz).astimezone(UTC)


def scan(path: Path, device_id: str, tz: ZoneInfo, boundary_hour: int) -> list[SessionRef]:
    refs = []
    for prefix, files in list_groups(path).items():
        start = prefix_start(prefix, tz)
        paths = sorted(files.values(), key=lambda p: p.name)
        refs.append(
            SessionRef(
                id=session_id(device_id, start),
                therapy_day=therapy_day(start, tz, boundary_hour),
                start=start,
                fingerprint=fingerprint(paths),
                files=tuple(paths),
            )
        )
    return sorted(refs, key=lambda r: r.start)


def _local_to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    return naive.replace(tzinfo=tz).astimezone(UTC)


def _warn(warnings: list[str], msg: str) -> None:
    if msg not in warnings:
        warnings.append(msg)


def load_session(
    path: Path,
    ref: SessionRef,
    device_id: str,
    tz: ZoneInfo,
    boundary_hour: int,
    *,
    newest: bool,
    growing_window: timedelta,
    now: datetime,
    warnings: list[str],
) -> Session:
    channels: list[ChannelData] = []
    events: list[Event] = []
    seen_events: set[tuple[datetime, EventType]] = set()
    newest_mtime = 0.0
    for f in ref.files:
        newest_mtime = max(newest_mtime, f.stat().st_mtime)
        kind = _NAME.match(f.name).group(3).upper()  # type: ignore[union-attr]
        edf = EdfFile.open(f)
        for w in edf.warnings:
            _warn(warnings, f"{f.name}: {w}")
        if kind in SIGNAL_TYPES:
            start = _local_to_utc(edf.start, tz)
            for label, hdr in edf.signals.items():
                if label not in CHANNEL_MAP:
                    _warn(warnings, f"unmapped ResMed channel {label!r} ({kind})")
                    continue
                name, scale = CHANNEL_MAP[label]
                values = edf.read_signal(label).astype(np.float64) * scale
                if name in SANITIZERS:
                    values = SANITIZERS[name](values)
                from pap_analytics.model import CHANNELS

                channels.append(
                    ChannelData(name, CHANNELS[name], hdr.rate_hz, start, values.astype(np.float32))
                )
        elif kind in EVENT_TYPES:
            for a in edf.annotations():
                etype = map_event(a.text)
                if etype is None:
                    _warn(warnings, f"unmapped ResMed event {a.text!r} ({kind})")
                    etype = EventType.OTHER
                ts = _local_to_utc(a.start, tz)
                if etype is EventType.RECORDING_START and (ts, etype) in seen_events:
                    continue
                seen_events.add((ts, etype))
                events.append(Event(ts, a.duration_s or 0.0, etype, a.text, Provenance.DEVICE))
        else:
            _warn(warnings, f"unknown ResMed file type {kind!r}")
    ends = [c.start + timedelta(seconds=len(c.values) / c.rate_hz) for c in channels if c.rate_hz > 0]
    end = (
        max(ends) if ends else max([e.start + timedelta(seconds=e.duration_s) for e in events] + [ref.start])
    )
    events.sort(key=lambda e: e.start)
    complete = not (newest and now - datetime.fromtimestamp(newest_mtime, UTC) < growing_window)
    return Session(
        id=ref.id,
        device_id=device_id,
        therapy_day=therapy_day(ref.start, tz, boundary_hour),
        start=ref.start,
        end=end,
        complete=complete,
        fingerprint=ref.fingerprint,
        source_files=tuple(str(p.relative_to(path)) for p in ref.files),
        channels=tuple(channels),
        events=tuple(events),
    )
