# WP-03 – ResMed adapter

**Depends on:** WP-02 (and WP-01 contracts) · **Unblocks:** WP-05

## Goal
Implement the `Adapter` protocol for the ResMed AirSense/AirCurve 10/11 SD-card layout (also produced by the AirCANnect SMB share), returning canonical objects.

## Scope
1. `detect`: directory contains `STR.edf` and `DATALOG/`, plus `Identification.json` (AirSense 11) or `Identification.tgt` (AirSense 10).
2. `device`: from Identification (`FlowGenerator.IdentificationProfiles.Product`: `ProductName`, `ProductCode`, `SerialNumber`; firmware from software identifiers if present). `id = f"resmed-{serial}"`, `source_kind = "resmed-sd"`. TGT parser for AS10 (`#SRN`, `#PNA`, `#PCD` keys) – synthetic test only.
3. `scan`: group `DATALOG/*/<YYYYMMDD_HHMMSS>_<TYPE>.edf` by prefix → `SessionRef`; start = timestamp from file name interpreted in source tz; `therapy_day` via `model.therapy_day` (do **not** trust the folder name; record in a test how the folder relates to the therapy day for 20261026/20261028). Fingerprint = sha256 of sorted (name, size, mtime_ns).
4. `load_session`: read BRP/PLD/SA2 signals, EVE/CSL annotations; map to canonical channels/events (table below); convert units; channel `start` from the EDF header start (local → UTC); `end` = max channel end; `complete = False` if the session is the newest one of the source and its files were modified < 15 min ago (constant, overridable for tests) – otherwise True.
5. `therapy_days` (device metrics) and `settings` from `STR.edf`: one row per record; `Date` = days since 1970-01-01; skip/flag values < 0 as "not available"; usage from `Duration` (min); mask intervals from `MaskOn`/`MaskOff` (minutes from noon, `-1` = unused). Metrics (provenance `device`): `ahi`, `hi`, `ai`, `oai`, `cai`, `uai`, `rin` (if present), `leak_p50`, `leak_p95`, `leak_max`, `mask_pressure_p50/p95/max`, `resp_rate_p50/p95`, `tidal_volume_p50/p95` (mL), `minute_vent_p50/p95`, `spo2_p50/p95/min` (when ≥ 0), `csr_minutes`. Settings: mode (map ResMed mode code → canonical name; code 6 seen in data – identify via `edf_resscan.py`/OSCAR source), min/max pressure, EPR level/type, ramp, humidity/climate, mask type – store all STR `S.*` fields in `raw`.
6. `warnings` for unmapped labels/annotation texts.

### Mapping (ResMed → canonical)
| Raw | Canonical | Conversion |
|---|---|---|
| `Flow.40ms` | `flow` | L/s |
| `Press.2s` (PLD) | `pressure` | cmH2O, 0.5 Hz |
| `Press.40ms` (BRP) | `pressure_hr` | cmH2O, 25 Hz |
| `MaskPress.2s` | `mask_pressure` | cmH2O |
| `EprPress.2s` | `epap` | cmH2O |
| `Leak.2s` | `leak` | L/s |
| `RespRate.2s` | `resp_rate` | 1/min |
| `TidVol.2s` | `tidal_volume` | L → **mL** (×1000) |
| `MinVent.2s` | `minute_vent` | L/min |
| `TgtVent.2s` | `target_vent` | L/min |
| `IERatio.2s` | `ie_ratio` | % |
| `Snore.2s` | `snore` | vendor scale |
| `FlowLim.2s` | `flow_limitation` | 0–1 |
| `Ti.2s` | `ti` | s |
| `SpO2.1s` | `spo2` | %, values outside 50–100 → NaN |
| `Pulse.1s` | `pulse` | 1/min, values ≤ 0 → NaN |
| EVE `Hypopnea` | `hypopnea` | |
| EVE `Obstructive Apnea` | `obstructive_apnea` | |
| EVE `Central Apnea` | `central_apnea` | |
| EVE `Apnea` | `apnea` | unclassified |
| EVE `Arousal` | `arousal` | |
| EVE/CSL `Recording starts` | `recording_start` | |
| CSL `Cheyne-Stokes…`/`CSR` spans | `csr` | |

Pressure (per ARCHITECTURE): `pressure` is the 0.5 Hz channel used for statistics; `pressure_hr` is the 25 Hz channel for waveform windows only.

## Out of scope
Storage, ingest scheduling, analytics/derived metrics.

## Files
`src/pap_analytics/adapters/resmed/{__init__,detect,identification,str_summary,datalog,mapping}.py`, `tests/test_resmed_*.py`.

## Acceptance (real data)
- `detect` true for both fixture sets; device = AirSense11AutoSet, code 39444, serial 23262018278.
- `scan(resmed-air11-autoset)`: 46 sessions (verified at bootstrap: 46 distinct prefixes = 46 `Recording starts`), session prefixes for 20261108 = `203013`, `233938`, both on therapy day 2026-11-08.
- `load_session` of `20261108_233938`: channels incl. `spo2`, `pulse`, `flow` (25 Hz), `pressure` (0.5 Hz); `tidal_volume` in mL (median in plausible 300–800 range); UTC start = 2026-11-08 22:39:38Z (CET, UTC+1).
- STR history set: 10 therapy days 2026-10-16…25 with `ahi`/`usage` exactly as in `tests/data/README.md` table (2026-10-18 usage 1 min, AHI n/a → `value=None`).
- STR main set: 1 therapy day 2026-11-08, usage 383 min, AHI 1.0, leak_p95 0.14 L/s, settings min 4 / max 20 cmH2O, EPR 3.
- Unknown label in a synthetic file → appears in `warnings`, not an exception.
