# WP-05 – Incremental ingest, `pap ingest`, `pap doctor`

**Depends on:** WP-03, WP-04 · **Unblocks:** WP-06, WP-07

## Goal
Bring a source directory into the store incrementally and idempotently; give a human a quick diagnostic view.

## Scope
1. `ingest.py`: `ingest(config, *, source: Path | None = None, rebuild: bool = False) -> IngestReport`:
   - for each configured source (or the given one): `adapters.detect` → `device` → `scan`;
   - compare fingerprints with `store.session_fingerprints`; load + `replace_session` only new/changed sessions; sessions whose files disappeared from the source are **kept** (source may be a pruned SD card) unless `--prune`;
   - refresh `therapy_days` and `settings` from the source as upserts keyed by (device, date); days already in the store but absent from the current source are kept (same rule as sessions, removed only with `--prune`);
   - per-session failure → logged in report + `ingest_log`, ingest continues;
   - holds the store write lock for the whole run.
2. CLI `pap ingest [--source PATH] [--rebuild] [--prune] [--json]` – human summary or JSON report.
3. CLI `pap doctor PATH [--json]` – no store needed: detected adapter, device, number of days/sessions, channels per file type, oximetry presence per day, STR day range, adapter warnings.
4. Optional `--watch SECONDS` loop for ingest (simple sleep loop; used by Docker in WP-10).

## Contract
```python
@dataclass
class IngestReport:
    source: str; device_id: str | None
    sessions_added: int; sessions_updated: int; sessions_unchanged: int; sessions_failed: int
    therapy_days: int; warnings: list[str]; errors: list[str]; duration_s: float
def ingest(config: Config, *, source: Path | None = None, rebuild: bool = False,
           prune: bool = False) -> list[IngestReport]: ...
```

## Out of scope
Scheduling on a server (WP-10), analytics.

## Files
`src/pap_analytics/ingest.py`, `src/pap_analytics/cli_ingest.py` (subcommands), `tests/test_ingest.py`, `tests/test_cli_doctor.py`.

## Acceptance (real data; tests copy fixture to `tmp_path` when they need to mutate it)
- First ingest of `resmed-air11-autoset`: sessions_added = number from WP-03 scan, failed = 0.
- Second ingest: all unchanged, < 3 s on CI.
- Touching (mtime) one session's PLD → exactly one updated.
- Ingest `resmed-air11-str-20261016` STR then `resmed-air11-autoset`: store has device days 2026-10-16…25 **and** 2026-11-08 (upsert keeps days from both sources).
- `pap doctor tests/data/resmed-air11-autoset --json` lists device AirSense11AutoSet, 21 folders, oximetry on the 7 days listed in `tests/data/README.md`.
- Source directory unchanged after ingest (compare checksums).
