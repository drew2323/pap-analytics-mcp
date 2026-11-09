# Test data

Real therapy data of the repository author, **published deliberately as test fixtures** (see ADR-006). Immutable: do not modify, regenerate, anonymise or delete. Verify integrity with `sha256sum -c SHA256SUMS` inside each set.

## `resmed-air11-autoset/` – main set

Snapshot of the AirCANnect SMB share (`hnizdo:/srv/aircannect`) taken 2026-11-09, identical to a ResMed SD-card layout.

- Device (`Identification.json`): ResMed **AirSense11AutoSet**, product code `39444`, serial `23262018278`.
- Layout: `Identification.json` + `Identification.crc`, `STR.edf`, `DATALOG/YYYYMMDD/YYYYMMDD_HHMMSS_{BRP,PLD,EVE,CSL,SA2}.edf` (+ `.crc` files).
- 21 DATALOG folders `20261016`–`20261108` (no `20261018`, `20261031`, `20261101`), 215 EDF files in DATALOG + `STR.edf`, 43 sessions, ~35 MB.

Signals (labels as in EDF headers, every file additionally ends with a `Crc16` pseudo-signal):

| File | Rate | Signals |
|---|---|---|
Record duration 60 s for BRP/PLD/SA2. Units as in the EDF header:

| File | Rate | Signals (unit) |
|---|---|---|
| BRP | 25 Hz | `Flow.40ms` (L/s), `Press.40ms` (cmH2O) |
| PLD | 0.5 Hz | `MaskPress.2s`, `Press.2s`, `EprPress.2s` (cmH2O), `Leak.2s` (L/s), `RespRate.2s` (bpm), `TidVol.2s` (**L**), `MinVent.2s`, `TgtVent.2s` (L/min), `IERatio.2s` (%), `Snore.2s`, `FlowLim.2s` (unitless), `Ti.2s` (seconds) |
| SA2 | 1 Hz | `Pulse.1s` (bpm), `SpO2.1s` (%) – oximeter; `-1` = no sensor/invalid |
| EVE | EDF+D annotations (record duration 0) | `Hypopnea` (129), `Apnea` (19), `Central Apnea` (1), `Arousal` (1), `Recording starts` (43) – counts over the whole set; one TAL fragment (`ö+0`) must be parsed, not treated as events |
| CSL | EDF+D annotations | only `Recording starts` (no Cheyne-Stokes spans in this set) |

**Oximetry (SpO2 valid 50–100 %, seconds valid/total, minimum):** 20261016 20807/20880 min 96 · 20261017 25763/25980 min 94 · 20261019 24991/25080 min 95 · 20261020 21449/21660 min 94 · 20261021 25292/25680 min 92 · 20261103 21112/21780 min 95 · 20261108 22549/22860 min 89. Other nights: SA2 present but no valid SpO2.

**Sessions per DATALOG folder** (session = shared `YYYYMMDD_HHMMSS` prefix; PLD duration):

| Folder | Sessions | PLD min | | Folder | Sessions | PLD min |
|---|---|---|---|---|---|---|
| 20261016 | 2 | 348 | | 20261028 | 1 | 24 |
| 20261017 | 6 | 433 | | 20261029 | 1 | 57 |
| 20261019 | 1 | 418 | | 20261030 | 2 | 44 |
| 20261020 | 4 | 361 | | 20261102 | 1 | 48 |
| 20261021 | 3 | 428 | | 20261103 | 7 | 367 |
| 20261022 | 1 | 434 | | 20261104 | 1 | 410 |
| 20261023 | 2 | 388 | | 20261105 | 1 | 376 |
| 20261024 | 1 | 404 | | 20261106 | 1 | 386 |
| 20261025 | 2 | 354 | | 20261107 | 1 | 14 |
| 20261026 | 2 | 360 | | 20261108 | 2 (`203013`, `233938`) | 381 |
| 20261027 | 1 | 64 | | |  | |

Note: folder 20261026 starts at `003639` and 20261028 at `002108`. To be checked in WP-03: does the folder name follow the noon-to-noon therapy day convention, or the file start date?

**`STR.edf` in this set holds 1 record (therapy day 2026-11-08):** Duration 383 min, AHI 1.0, HI 0.9, AI 0.1, Leak.95 0.14 L/s, MaskPress.95 4.64 cmH2O, S.A.MinPress 4, S.A.MaxPress 20, EPR level 3, SpO2.50 −1 (STR does not carry oximetry). Values ≤ −0.1 / −1 mean "not available".
## `resmed-air11-str-20261016/` – older STR snapshot

The same device. Contains `STR.edf` with 10 daily records 2026-10-16 … 2026-10-25, plus Identification. Use it for STR parsing/settings tests and as the `device` reference for these days. Device-reported reference values for tests (Date = days since 1970-01-01):

| Day | Duration min | AHI | HI | AI | Leak.95 L/s | MaskPress.95 |
|---|---|---|---|---|---|---|
| 2026-10-16 | 349 | 1.2 | 1.0 | 0.1 | 0.72 | 6.72 |
| 2026-10-17 | 434 | 0.4 | 0.4 | 0.0 | 0.74 | 6.72 |
| 2026-10-19 | 419 | 2.0 | 1.8 | 0.1 | 0.62 | 6.72 |
| 2026-10-20 | 362 | 4.3 | 3.4 | 0.8 | 0.64 | 6.56 |
| 2026-10-21 | 430 | 2.9 | 2.5 | 0.4 | 0.82 | 6.88 |
| 2026-10-22 | 435 | 4.6 | 3.8 | 0.8 | 0.56 | 6.72 |
| 2026-10-23 | 390 | 1.5 | 1.3 | 0.1 | 0.24 | 5.76 |
| 2026-10-24 | 404 | 0.4 | 0.2 | 0.1 | 0.26 | 5.60 |
| 2026-10-25 | 355 | 0.5 | 0.5 | 0.0 | 0.14 | 5.60 |

These values were decoded with an independent throw-away script during project bootstrap. Before relying on them, WP-02/WP-03 must cross-check them against OSCAR.
