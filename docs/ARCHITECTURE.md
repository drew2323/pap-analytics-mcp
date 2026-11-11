# Architecture

Stable shape of the system. No history – decisions with rationale are in [DECISIONS.md](DECISIONS.md), implementation specs in [plan/](plan/README.md).

## Purpose and boundaries

An MCP server that lets AI agents query and analyse PAP therapy data. It **reads** vendor data, **normalises** it into a canonical model, **stores** it locally and **answers** aggregate questions.

Not in scope: writing to devices or source media, firmware, replacing device events as the AHI reference (own analyses such as breath-by-breath flow analysis are allowed when they add clear insight; they stay `derived`, separate and never enter AHI – ADR-011), sleep staging, clinical diagnosis, multi-user hosting.

## Components

```
src/pap_analytics/
  edf/          generic EDF/EDF+ reader (no vendor knowledge)
  adapters/     vendor adapters: raw files → canonical model
    base.py       Adapter protocol + registry/auto-detection
    resmed/       ResMed AirSense/AirCurve 10/11 SD-card layout
  model.py      canonical PAP data model + SCHEMA_VERSION
  store/        persistence of the canonical model (DuckDB catalog + Parquet signals)
  ingest.py     incremental import: source → adapter → store
  analytics/    pure functions over the store / canonical model
  mcp_server.py MCP tools/resources/prompts (thin layer over analytics)
  cli.py        `pap` CLI (doctor, ingest, query commands, `serve`)
  config.py     configuration (TOML file + env overrides; `sources[].sync` block)
  sync.py       optional rsync pull of a source into its local path (used by ingest/CLI only)
```

Dependency direction (strict, enforced by review): `edf` ← `adapters` → `model` ← `store` ← `analytics` ← `mcp_server`/`cli`. Analytics and MCP **never import adapters or vendor code**; adapters never import store/analytics.

## Data flow

1. **Detect:** `adapters.detect(path)` asks each registered adapter whether it recognises the source directory.
2. **Scan:** the adapter lists sessions with a cheap fingerprint per session (file names, sizes, mtimes, header hash).
3. **Load:** for new/changed sessions the adapter returns canonical `Session` objects with channels (numpy arrays) and events, plus `TherapyDay` summaries and `Settings` where the vendor provides them.
4. **Persist:** `store` writes catalog rows and per-channel Parquet files; a session is replaced atomically on re-ingest.
5. **Analyse/serve:** analytics read from the store; MCP tools return compact JSON.

## Canonical model (contract, `model.py`)

Vendor-neutral; units fixed below. Every persisted change bumps `SCHEMA_VERSION` (store refuses mismatched data and asks for re-ingest).

| Entity | Key fields |
|---|---|
| `Device` | `id` (vendor+serial), `vendor`, `model`, `model_code`, `serial`, `firmware`, `source_kind` |
| `Session` | `id`, `device_id`, `therapy_day`, `start`, `end` (tz-aware), `complete: bool`, `fingerprint`, `source_files`, `mode` (therapy mode the session ran in, from its recorded data; `None` = unknown) |
| `Channel` | `session_id`, `name` (canonical), `unit`, `rate_hz`, `start`, `values: float32[]` (NaN = missing) |
| `Event` | `session_id`, `start`, `duration_s`, `type` (canonical), `raw_type`, `provenance` |
| `TherapyDay` | `device_id`, `date`, `usage_s`, `mask_intervals`, `metrics: dict[str, Metric]` |
| `Settings` | `device_id`, `valid_from`, `mode`, `values: dict` (canonical keys), `raw: dict` |
| `Metric` | `value`, `unit`, `provenance` (`device` \| `derived`), `method` (short text) |

**Canonical channels:** `flow` L/s, `pressure` cmH2O (device delivered, low-rate, used for statistics), `pressure_hr` cmH2O (same at waveform rate, windows only), `mask_pressure` cmH2O, `epap` cmH2O, `leak` L/s, `resp_rate` 1/min, `tidal_volume` mL, `minute_vent` L/min, `target_vent` L/min, `ie_ratio`, `ti` s, `snore` (vendor scale), `flow_limitation` 0–1, `spo2` %, `pulse` 1/min.

**Canonical event types:** `obstructive_apnea`, `central_apnea`, `apnea` (unclassified), `hypopnea`, `rera`, `arousal`, `flow_limitation`, `csr` (span), `periodic_breathing` (span), `large_leak` (span), `desaturation` (derived only), `recording_start`, `other` (with `raw_type`).

**Therapy mode (invariant):** the mode is taken from the data per session (`Session.mode`), not from the device model and not from the newest settings – a device may run any mode and be switched between nights. `Settings` are change points known only for days the vendor summary covers; analytics mark settings whose mode differs from the night's session mode as not applying (`applies: false` + note) instead of presenting them as that night's settings.

Unknown raw channels/events are never silently dropped: they map to `other`/are listed in the adapter's `warnings`.

## Time model (invariant)

- All stored timestamps are timezone-aware UTC. Vendor local times are converted with the configured device timezone (default `Europe/Prague`).
- **Therapy day** = the local date on which the 12:00→12:00 window starts (ResMed/OSCAR convention; boundary configurable). A night from 22:00 to 06:00 belongs to the evening's date.
- Query APIs accept local times; segment times like `02:10` resolve within the therapy day window.

## Provenance (invariant)

Every metric returned by analytics/MCP carries `provenance`: `device` = value reported by the device (e.g. ResMed STR AHI, EVE events), `derived` = computed by this tool (e.g. leak p95 from the 2 s channel, ODI). Derived metrics never overwrite device metrics; both may be returned side by side.

## Store (contract)

- One store directory: `catalog.duckdb` + `signals/<device_id>/<session_id>/<channel>.parquet`.
- Catalog tables: `meta(schema_version)`, `devices`, `sessions`, `channels`, `events`, `therapy_days`, `day_metrics`, `settings`, `ingest_log`.
- Ingest is idempotent; the newest (possibly still growing) session is re-ingested when its fingerprint changes and stays `complete=false` while it may grow.
- Single writer (ingest holds a lock file); the MCP server opens the store read-only and sees new data after ingest commits. Readers retry briefly on a lock conflict, then return a clear "data is being updated" error.
- Outside the catalog, `sync_state.json` in the store directory records the last sync attempt per source (not part of the canonical model; disposable).

## MCP surface (contract)

Tools return compact JSON with explicit units and provenance; any time series is downsampled to ≤ `max_points` (default 300, hard cap 2000) using bucket min/mean/max. No tool returns raw high-rate data.

`list_devices`, `list_nights`, `night_summary`, `get_events`, `segment_stats`, `get_signal`, `get_signals`, `find_anomalies`, `compare_periods`, `get_settings`, `oximetry_summary`, `disturbance_summary`, `disturbance_trend`, optional `plot_segment`, admin `ingest` (disabled with `--read-only`). Resources: `pap://glossary`, `pap://devices`. Prompt: `nightly_review`.

Disturbance results keep `measured` metrics (read off a signal: pulse response, pulse rises, hypoxic burden, flow limitation, calm/restless breathing from the 25 Hz flow with its coverage) apart from `inferred` ones (possible arousals, RERA candidates); both are `derived`.

Exact signatures: [plan/WP-08-mcp-server.md](plan/WP-08-mcp-server.md).

## Runtime and security

- Transports: stdio (local agents) and streamable HTTP. HTTP binds `127.0.0.1` by default and requires a bearer token (fail-closed); intended exposure is tailnet only.
- Source directories are mounted/opened read-only. The store is the only thing written.
- Same code and the same TOML config run natively and in Docker (two equal runtimes; Docker adds only mounts and `PAP_*` env). Ingest reads only the local `path`; the optional `[sources.sync]` (rsync over SSH, pull only, strict host key) copies into it and never writes to the remote. The MCP service mounts source and store read-only.
- Secrets (HTTP token, SSH key) are referenced by file path; none are in the repo, image or config file. Production runs from a released tag.
- Deployment specifics (hosts, sync access, install/rollback, clients): [OPERATIONS.md](OPERATIONS.md).
