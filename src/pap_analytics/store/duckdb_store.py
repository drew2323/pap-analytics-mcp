"""DuckDB catalog + Parquet signals (see docs/ARCHITECTURE.md, "Store").

The writer holds ``store.lock`` for its lifetime. DuckDB connections are short-lived (one per
operation), so a read-only ``Store`` in another process can query between writer operations.
Timestamps are stored as naive UTC ``TIMESTAMP`` and returned tz-aware (UTC).
"""

from __future__ import annotations

import json
import math
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from pap_analytics.model import (
    SCHEMA_VERSION,
    ChannelData,
    Device,
    Event,
    EventType,
    Metric,
    Provenance,
    Session,
    Settings,
    TherapyDay,
)
from pap_analytics.store import parquet_io
from pap_analytics.store.lock import WriterLock
from pap_analytics.store.types import (
    IngestLogEntry,
    SchemaMismatchError,
    SessionInfo,
    StoreLockedError,
    StoreReadOnlyError,
)

CATALOG = "catalog.duckdb"

_DDL = """
CREATE TABLE meta(schema_version INTEGER);
CREATE TABLE devices(id VARCHAR PRIMARY KEY, vendor VARCHAR, model VARCHAR, model_code VARCHAR,
    serial VARCHAR, firmware VARCHAR, source_kind VARCHAR);
CREATE TABLE sessions(id VARCHAR PRIMARY KEY, device_id VARCHAR, therapy_day DATE, start TIMESTAMP,
    "end" TIMESTAMP, complete BOOLEAN, fingerprint VARCHAR, source_files VARCHAR);
CREATE TABLE channels(session_id VARCHAR, name VARCHAR, unit VARCHAR, rate_hz DOUBLE, start TIMESTAMP,
    n_samples BIGINT, path VARCHAR);
CREATE TABLE events(session_id VARCHAR, device_id VARCHAR, start TIMESTAMP, duration_s DOUBLE,
    type VARCHAR, raw_type VARCHAR, provenance VARCHAR);
CREATE TABLE therapy_days(device_id VARCHAR, date DATE, usage_s DOUBLE, mask_intervals VARCHAR);
CREATE TABLE day_metrics(device_id VARCHAR, date DATE, key VARCHAR, value DOUBLE, unit VARCHAR,
    provenance VARCHAR, method VARCHAR);
CREATE TABLE settings(device_id VARCHAR, valid_from DATE, mode VARCHAR, "values" VARCHAR, raw VARCHAR);
CREATE TABLE ingest_log(started TIMESTAMP, finished TIMESTAMP, source VARCHAR, device_id VARCHAR,
    sessions_new INTEGER, sessions_updated INTEGER, sessions_unchanged INTEGER, warnings VARCHAR,
    error VARCHAR);
CREATE INDEX events_dev_start ON events(device_id, start);
CREATE INDEX channels_session ON channels(session_id);
"""


def _db(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(UTC).replace(tzinfo=None)


def _py(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC)


def safe_name(session_id: str) -> str:
    return session_id.replace(":", "_").replace("/", "_")


READ_RETRIES = 5  # bounded retry for readers while a writer commits
READ_RETRY_S = 0.2
WRITE_RETRIES = 200
WRITE_RETRY_S = 0.1


class Store:
    def __init__(self, store_dir: Path, read_only: bool, lock: WriterLock | None) -> None:
        self.dir = store_dir
        self.read_only = read_only
        self._lock = lock

    # ---- lifecycle -------------------------------------------------------------------------
    @classmethod
    def open(cls, store_dir: Path, *, read_only: bool) -> Store:
        store_dir = Path(store_dir)
        if read_only:
            if not (store_dir / CATALOG).exists():
                raise FileNotFoundError(f"no store at {store_dir} (run `pap ingest` first)")
            store = cls(store_dir, True, None)
        else:
            store_dir.mkdir(parents=True, exist_ok=True)
            lock = WriterLock(store_dir / "store.lock")
            lock.acquire()
            store = cls(store_dir, False, lock)
            try:
                if not (store_dir / CATALOG).exists():
                    store._init_schema()
            except BaseException:
                lock.release()
                raise
        try:
            store._check_version()
        except BaseException:
            store.close()
            raise
        return store

    def close(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._conn() as con:
            con.execute(_DDL)
            con.execute("INSERT INTO meta VALUES (?)", [SCHEMA_VERSION])

    def _check_version(self) -> None:
        with self._conn() as con:
            row = con.execute("SELECT schema_version FROM meta").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            found = None if row is None else row[0]
            raise SchemaMismatchError(
                f"store schema version {found} != expected {SCHEMA_VERSION}; run `pap ingest --rebuild`"
            )

    def rebuild(self) -> None:
        """Wipe catalog and signals (only inside ``store_dir``) and recreate an empty schema."""
        self._require_writable()
        for name in (CATALOG, CATALOG + ".wal"):
            (self.dir / name).unlink(missing_ok=True)
        shutil.rmtree(self.dir / "signals", ignore_errors=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[duckdb.DuckDBPyConnection]:
        path = str(self.dir / CATALOG)
        # Readers: bounded retry, then a clear error. Writer: DuckDB refuses to open for writing while a
        # reader process holds the file (readers hold it only per query), so retry for longer.
        retries, delay = (READ_RETRIES, READ_RETRY_S) if self.read_only else (WRITE_RETRIES, WRITE_RETRY_S)
        for attempt in range(retries):
            try:
                con = duckdb.connect(path, read_only=self.read_only)
                break
            except duckdb.IOException as exc:
                if attempt == retries - 1:
                    if self.read_only:
                        raise StoreLockedError("data is being updated, retry in a few seconds") from exc
                    raise StoreLockedError(f"store is busy (readers hold it): {exc}") from exc
                time.sleep(delay)
        try:
            yield con
        finally:
            con.close()

    def _require_writable(self) -> None:
        if self.read_only:
            raise StoreReadOnlyError("store is open read-only")

    # ---- writes ----------------------------------------------------------------------------
    def upsert_device(self, d: Device) -> None:
        self._require_writable()
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO devices VALUES (?,?,?,?,?,?,?)",
                [d.id, d.vendor, d.model, d.model_code, d.serial, d.firmware, d.source_kind],
            )

    def replace_session(self, s: Session) -> None:
        """Atomically replace a session: new Parquet files are staged, swapped in, catalog committed."""
        self._require_writable()
        sig_dir = self.dir / "signals" / s.device_id
        sig_dir.mkdir(parents=True, exist_ok=True)
        final = sig_dir / safe_name(s.id)
        staging = sig_dir / f".tmp-{uuid.uuid4().hex}"
        trash = sig_dir / f".old-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            rows = []
            for ch in s.channels:
                n = parquet_io.write_channel(
                    staging / f"{ch.name}.parquet", ch.values, ch.rate_hz, ch.start, ch.unit
                )
                rel = (final / f"{ch.name}.parquet").relative_to(self.dir).as_posix()
                rows.append([s.id, ch.name, ch.unit, ch.rate_hz, _db(ch.start), n, rel])
            moved = False
            with self._conn() as con:
                con.execute("BEGIN")
                try:
                    self._delete_rows(con, s.id)
                    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                                [s.id, s.device_id, s.therapy_day, _db(s.start), _db(s.end), s.complete,
                                 s.fingerprint, json.dumps(list(s.source_files))])  # fmt: skip
                    if rows:
                        con.executemany("INSERT INTO channels VALUES (?,?,?,?,?,?,?)", rows)
                    if s.events:
                        con.executemany(
                            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                            [[s.id, s.device_id, _db(e.start), e.duration_s, e.type.value, e.raw_type,
                              e.provenance.value] for e in s.events],
                        )  # fmt: skip
                    if final.exists():
                        final.rename(trash)
                    staging.rename(final)
                    moved = True
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    if moved:
                        final.rename(staging)
                    if trash.exists():
                        trash.rename(final)
                    raise
            shutil.rmtree(trash, ignore_errors=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _delete_rows(con: duckdb.DuckDBPyConnection, session_id: str) -> None:
        for table, col in (("channels", "session_id"), ("events", "session_id"), ("sessions", "id")):
            con.execute(f"DELETE FROM {table} WHERE {col} = ?", [session_id])

    def delete_session(self, session_id: str) -> None:
        self._require_writable()
        with self._conn() as con:
            row = con.execute("SELECT device_id FROM sessions WHERE id = ?", [session_id]).fetchone()
            self._delete_rows(con, session_id)
        if row is not None:
            shutil.rmtree(self.dir / "signals" / row[0] / safe_name(session_id), ignore_errors=True)

    def replace_therapy_days(self, device_id: str, days: list[TherapyDay]) -> None:
        self._require_writable()
        with self._conn() as con:
            con.execute("BEGIN")
            con.execute("DELETE FROM therapy_days WHERE device_id = ?", [device_id])
            con.execute("DELETE FROM day_metrics WHERE device_id = ?", [device_id])
            for d in days:
                con.execute("INSERT INTO therapy_days VALUES (?,?,?,?)", [
                    device_id, d.date, d.usage_s,
                    json.dumps([[a.isoformat(), b.isoformat()] for a, b in d.mask_intervals]),
                ])  # fmt: skip
                if d.metrics:
                    con.executemany(
                        "INSERT INTO day_metrics VALUES (?,?,?,?,?,?,?)",
                        [[device_id, d.date, k, m.value, m.unit, m.provenance.value, m.method]
                         for k, m in d.metrics.items()],
                    )  # fmt: skip
            con.execute("COMMIT")

    def replace_settings(self, device_id: str, settings: list[Settings]) -> None:
        self._require_writable()
        with self._conn() as con:
            con.execute("BEGIN")
            con.execute("DELETE FROM settings WHERE device_id = ?", [device_id])
            for st in settings:
                con.execute(
                    "INSERT INTO settings VALUES (?,?,?,?,?)",
                    [
                        device_id,
                        st.valid_from,
                        st.mode,
                        json.dumps(st.values),
                        json.dumps(st.raw),
                    ],
                )
            con.execute("COMMIT")

    def log_ingest(self, e: IngestLogEntry) -> None:
        self._require_writable()
        with self._conn() as con:
            con.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?,?,?,?,?)", [
                _db(e.started), _db(e.finished), e.source, e.device_id, e.sessions_new,
                e.sessions_updated, e.sessions_unchanged, json.dumps(list(e.warnings)), e.error])  # fmt: skip

    # ---- reads -----------------------------------------------------------------------------
    def devices(self) -> list[Device]:
        with self._conn() as con:
            rows = con.execute("SELECT * FROM devices ORDER BY id").fetchall()
        return [Device(*r) for r in rows]

    def sessions(
        self, device_id: str | None = None, day_from: date | None = None, day_to: date | None = None
    ) -> list[SessionInfo]:
        where, args = ["1=1"], []
        if device_id is not None:
            where.append("s.device_id = ?")
            args.append(device_id)
        if day_from is not None:
            where.append("s.therapy_day >= ?")
            args.append(day_from)
        if day_to is not None:
            where.append("s.therapy_day <= ?")
            args.append(day_to)
        sql = (
            'SELECT s.id, s.device_id, s.therapy_day, s.start, s."end", s.complete, s.fingerprint, '
            "s.source_files, COALESCE(list(c.name ORDER BY c.name) FILTER (WHERE c.name IS NOT NULL), []) "
            "FROM sessions s LEFT JOIN channels c ON c.session_id = s.id "
            f"WHERE {' AND '.join(where)} GROUP BY ALL ORDER BY s.start"
        )
        with self._conn() as con:
            rows = con.execute(sql, args).fetchall()
        return [
            SessionInfo(
                r[0], r[1], r[2], _py(r[3]), _py(r[4]), r[5], r[6], tuple(json.loads(r[7])), tuple(r[8])
            )
            for r in rows
        ]

    def session_fingerprints(self, device_id: str) -> dict[str, str]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT id, fingerprint FROM sessions WHERE device_id = ?", [device_id]
            ).fetchall()
        return dict(rows)

    def events(
        self, device_id: str, start: datetime, end: datetime, types: set[EventType] | None = None
    ) -> list[Event]:
        """Events overlapping ``[start, end)`` (a span event counts if it reaches into the window)."""
        sql = (
            "SELECT start, duration_s, type, raw_type, provenance FROM events "
            "WHERE device_id = ? AND start < ? "
            "AND start + to_microseconds(CAST(duration_s * 1e6 AS BIGINT)) >= ?"
        )
        args: list[object] = [device_id, _db(end), _db(start)]
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += sorted(t.value for t in types)
        with self._conn() as con:
            rows = con.execute(sql + " ORDER BY start", args).fetchall()
        return [Event(_py(r[0]), r[1], EventType(r[2]), r[3], Provenance(r[4])) for r in rows]

    def channel_window(
        self, device_id: str, channel: str, start: datetime, end: datetime
    ) -> list[ChannelData]:
        """Samples with timestamp in ``[start, end)``, one ``ChannelData`` per overlapping session."""
        sql = (
            "SELECT c.unit, c.rate_hz, c.start, c.n_samples, c.path FROM channels c "
            "JOIN sessions s ON s.id = c.session_id WHERE s.device_id = ? AND c.name = ? "
            "AND c.start < ? "
            "AND c.start + to_microseconds(CAST(c.n_samples / c.rate_hz * 1e6 AS BIGINT)) > ? "
            "ORDER BY c.start"
        )
        with self._conn() as con:
            rows = con.execute(sql, [device_id, channel, _db(end), _db(start)]).fetchall()
        out: list[ChannelData] = []
        for unit, rate, cstart, n, rel in rows:
            cstart = _py(cstart)
            i0 = max(0, math.ceil(_us(start - cstart) * rate / 1e6 - 1e-9))
            i1 = min(n, math.ceil(_us(end - cstart) * rate / 1e6 - 1e-9))
            if i1 <= i0:
                continue
            values = parquet_io.read_slice(self.dir / rel, i0, i1)
            out.append(ChannelData(channel, unit, rate, cstart + timedelta(seconds=i0 / rate), values))
        return out

    def therapy_days(self, device_id: str, day_from: date, day_to: date) -> list[TherapyDay]:
        with self._conn() as con:
            days = con.execute(
                "SELECT date, usage_s, mask_intervals FROM therapy_days "
                "WHERE device_id = ? AND date BETWEEN ? AND ? ORDER BY date",
                [device_id, day_from, day_to],
            ).fetchall()
            mrows = con.execute(
                "SELECT date, key, value, unit, provenance, method FROM day_metrics "
                "WHERE device_id = ? AND date BETWEEN ? AND ?",
                [device_id, day_from, day_to],
            ).fetchall()
        metrics: dict[date, dict[str, Metric]] = {}
        for d, key, value, unit, prov, method in mrows:
            metrics.setdefault(d, {})[key] = Metric(value, unit, Provenance(prov), method)
        return [
            TherapyDay(
                device_id,
                d,
                usage,
                tuple((datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in json.loads(mi)),
                metrics.get(d, {}),
            )
            for d, usage, mi in days
        ]

    def settings(self, device_id: str) -> list[Settings]:
        with self._conn() as con:
            rows = con.execute(
                'SELECT valid_from, mode, "values", raw FROM settings '
                "WHERE device_id = ? ORDER BY valid_from",
                [device_id],
            ).fetchall()
        return [Settings(device_id, vf, mode, json.loads(v), json.loads(r)) for vf, mode, v, r in rows]

    def last_ingest(self) -> IngestLogEntry | None:
        with self._conn() as con:
            r = con.execute("SELECT * FROM ingest_log ORDER BY finished DESC LIMIT 1").fetchone()
        if r is None:
            return None
        return IngestLogEntry(
            _py(r[0]), _py(r[1]), r[2], r[3], r[4], r[5], r[6], tuple(json.loads(r[7])), r[8]
        )


def _us(td: timedelta) -> int:
    return (td.days * 86400 + td.seconds) * 1_000_000 + td.microseconds


__all__ = ["Store"]
