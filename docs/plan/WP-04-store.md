# WP-04 – Store: DuckDB catalog + Parquet signals

**Depends on:** WP-01 · **Unblocks:** WP-05, WP-06, WP-07 · **Parallel with:** WP-02, WP-03

## Goal
Persist canonical objects and serve fast reads for analytics. Develop against synthetic canonical objects (no adapter needed).

## Scope
1. Dependencies: `duckdb`, `pyarrow`.
2. Layout per `docs/ARCHITECTURE.md` (Store): `catalog.duckdb` + `signals/<device_id>/<session_id>/<channel>.parquet` (session id sanitised for file names: replace `:` with `_`). Parquet: one column `v` float32 + file metadata `rate_hz`, `start` (ISO UTC), `unit`.
3. Catalog tables: `meta`, `devices`, `sessions`, `channels` (session_id, name, unit, rate_hz, start, n_samples, path), `events`, `therapy_days`, `day_metrics` (device_id, date, key, value, unit, provenance, method), `settings`, `ingest_log`.
4. Writes: `upsert_device`, `replace_session` (atomic: write Parquet to temp dir, then swap + catalog transaction; old files removed after commit), `replace_therapy_days`, `replace_settings`, `log_ingest`. Single-writer lock (`store.lock`, `fcntl.flock`).
5. Reads (read-only connection): listing/filtering by device, therapy day range, time window; channel window read returning numpy array + start time (Parquet row slicing via `rate_hz`, no full read when avoidable).
6. Schema version check: opening a store with different `SCHEMA_VERSION` raises `SchemaMismatchError` with a hint to run `pap ingest --rebuild`; `Store.rebuild()` wipes the store dir content safely (only inside `store_dir`).

## Contract
```python
class Store:
    @classmethod
    def open(cls, store_dir: Path, *, read_only: bool) -> "Store": ...
    # writes (read_only=False only; raise otherwise)
    def upsert_device(self, d: Device) -> None: ...
    def replace_session(self, s: Session) -> None: ...
    def delete_session(self, session_id: str) -> None: ...
    def replace_therapy_days(self, device_id: str, days: list[TherapyDay]) -> None: ...
    def replace_settings(self, device_id: str, settings: list[Settings]) -> None: ...
    def log_ingest(self, entry: IngestLogEntry) -> None: ...
    # reads
    def devices(self) -> list[Device]: ...
    def sessions(self, device_id: str | None = None, day_from: date | None = None,
                 day_to: date | None = None) -> list[SessionInfo]: ...      # Session without arrays + channel names
    def session_fingerprints(self, device_id: str) -> dict[str, str]: ...
    def events(self, device_id: str, start: datetime, end: datetime,
               types: set[EventType] | None = None) -> list[Event]: ...
    def channel_window(self, device_id: str, channel: str, start: datetime, end: datetime
                       ) -> list[ChannelData]: ...                         # one per overlapping session, sliced
    def therapy_days(self, device_id: str, day_from: date, day_to: date) -> list[TherapyDay]: ...
    def settings(self, device_id: str) -> list[Settings]: ...
    def last_ingest(self) -> IngestLogEntry | None: ...
```
`SessionInfo` and `IngestLogEntry` are defined in `store/types.py` (dataclasses, part of this contract).

## Out of scope
Deciding what to ingest (WP-05), computing metrics (WP-06/07).

## Files
`src/pap_analytics/store/{__init__,types,duckdb_store,parquet_io,lock}.py`, `tests/test_store.py`.

## Acceptance
- Round trip: synthetic session with 3 channels (25 Hz, 0.5 Hz, 1 Hz) and events → identical values (float32), events, metadata.
- `channel_window` returns exact slice (sample-accurate boundaries) and spans two sessions correctly; window outside data → `[]`.
- `replace_session` twice with the same id leaves one copy, no orphan Parquet files.
- Read-only store sees data committed by a writer after re-open; write on read-only raises.
- Concurrent writer is rejected by the lock (test with two processes or a held lock).
- Schema mismatch raises `SchemaMismatchError`.
- Performance smoke: window of 30 min from a 8 h 25 Hz channel reads in < 100 ms on CI (mark slow if flaky).
