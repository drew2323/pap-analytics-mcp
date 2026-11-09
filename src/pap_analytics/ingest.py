"""Incremental, idempotent import: source directory → adapter → store.

Sessions are matched by fingerprint; only new/changed ones are loaded. Data that disappeared
from the source (pruned SD card) is kept unless ``prune`` is set. Therapy days and settings are
upserted per (device, date). Boundaries: uses adapters only through the public protocol.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from pap_analytics import adapters
from pap_analytics.config import Config, SourceConfig
from pap_analytics.model import Settings, TherapyDay
from pap_analytics.store import IngestLogEntry, SchemaMismatchError, Store

log = logging.getLogger(__name__)
FAR_PAST, FAR_FUTURE = date(1900, 1, 1), date(9999, 12, 31)


@dataclass
class IngestReport:
    source: str
    device_id: str | None = None
    sessions_added: int = 0
    sessions_updated: int = 0
    sessions_unchanged: int = 0
    sessions_failed: int = 0
    therapy_days: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0


def _merge_days(old: list[TherapyDay], new: list[TherapyDay]) -> list[TherapyDay]:
    merged = {d.date: d for d in old}
    merged.update({d.date: d for d in new})
    return [merged[k] for k in sorted(merged)]


def _merge_settings(old: list[Settings], new: list[Settings]) -> list[Settings]:
    merged = {s.valid_from: s for s in old}
    merged.update({s.valid_from: s for s in new})
    out: list[Settings] = []
    for s in (merged[k] for k in sorted(merged)):  # drop consecutive duplicates (change-points only)
        if not out or (out[-1].mode, out[-1].values, out[-1].raw) != (s.mode, s.values, s.raw):
            out.append(s)
    return out


def _ingest_source(store: Store, src: SourceConfig, boundary: int, prune: bool) -> IngestReport:
    t0 = time.perf_counter()
    started = datetime.now(UTC)
    report = IngestReport(source=str(src.path))
    try:
        adapter = adapters.detect(src.path) if src.adapter == "auto" else adapters.get(src.adapter)
        device = adapter.device(src.path)
        report.device_id = device.id
        store.upsert_device(device)
        refs = adapter.scan(src.path, src.tz, boundary)
        known = store.session_fingerprints(device.id)
        for ref in refs:
            if known.get(ref.id) == ref.fingerprint:
                report.sessions_unchanged += 1
                continue
            try:
                store.replace_session(adapter.load_session(src.path, ref, src.tz, boundary))
            except Exception as exc:  # noqa: BLE001 - one bad session must not stop the run
                report.sessions_failed += 1
                report.errors.append(f"{ref.id}: {type(exc).__name__}: {exc}")
                log.exception("session %s failed", ref.id)
                continue
            if ref.id in known:
                report.sessions_updated += 1
            else:
                report.sessions_added += 1
        if prune:
            for sid in set(known) - {r.id for r in refs}:
                store.delete_session(sid)
        days = adapter.therapy_days(src.path, src.tz)
        settings = adapter.settings(src.path, src.tz)
        if prune:
            store.replace_therapy_days(device.id, days)
            store.replace_settings(device.id, settings)
        else:
            store.replace_therapy_days(
                device.id, _merge_days(store.therapy_days(device.id, FAR_PAST, FAR_FUTURE), days)
            )
            store.replace_settings(device.id, _merge_settings(store.settings(device.id), settings))
        report.therapy_days = len(days)
        report.warnings = list(dict.fromkeys(adapter.warnings))
        adapter.warnings.clear()
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"{type(exc).__name__}: {exc}")
        log.exception("ingest of %s failed", src.path)
    report.duration_s = time.perf_counter() - t0
    store.log_ingest(
        IngestLogEntry(
            started,
            datetime.now(UTC),
            report.source,
            report.device_id,
            report.sessions_added,
            report.sessions_updated,
            report.sessions_unchanged,
            tuple(report.warnings),
            "; ".join(report.errors) or None,
        )  # fmt: skip
    )
    return report


def ingest(
    config: Config, *, source: Path | None = None, rebuild: bool = False, prune: bool = False
) -> list[IngestReport]:
    """Ingest all configured sources (or just ``source``, using the first configured source's
    settings if its path matches, else defaults). Holds the store write lock for the whole run."""
    if source is not None:
        match = next((s for s in config.sources if s.path.resolve() == Path(source).resolve()), None)
        sources = [match or SourceConfig(path=Path(source))]
    else:
        sources = config.sources
    if rebuild:
        for name in ("catalog.duckdb", "catalog.duckdb.wal"):
            (config.store_dir / name).unlink(missing_ok=True)
    try:
        store = Store.open(config.store_dir, read_only=False)
    except SchemaMismatchError as exc:
        raise SchemaMismatchError(f"{exc} (use --rebuild)") from exc
    with store:
        if rebuild:
            store.rebuild()
        return [_ingest_source(store, s, config.day_boundary_hour, prune) for s in sources]
