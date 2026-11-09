# WP-02 – Generic EDF/EDF+ reader

**Depends on:** WP-01 · **Unblocks:** WP-03

## Goal
A vendor-neutral, read-only EDF/EDF+ reader that parses every file in `tests/data/` correctly.

## Scope
1. Header parsing (fixed 256 B + n×256 B signal headers): version, patient/recording ids, start date/time (EDF two-digit year: 85–99 → 19xx, else 20xx; also honour EDF+ `Startdate dd-MMM-yyyy` in recording id if present), number/duration of records, per-signal label, unit, physical/digital min/max, prefiltering, samples per record.
2. Signal reading into numpy (`int16` little endian → physical `float32` via linear scaling), lazily per signal (`read_signal(label)`), no full-file copy for big BRP files (use `numpy.memmap` or offset reads).
3. Handle **declared vs. actual record count**: `-1` or mismatch with file size → use complete records actually present, report truncation in `warnings`.
4. EDF+ annotations (`EDF Annotations` signal): parse TALs (`+onset[\x15duration]\x14text\x14...\x00`), timekeeping TAL (first, empty text) per record, multiple texts per TAL. Return onset relative to file start plus absolute datetime (naive, as stored).
5. ResMed quirk: signals labelled `Crc16` are excluded from `signals` (exposed only via `raw_signal_headers`). Optional CRC16 verification helper (algorithm per airbreak-plus `edf_resscan.py`, keep attribution comment).
6. Port useful pieces from `airbreak-plus/python/edf_resscan.py` (MIT, https://github.com/m-kozlowski/airbreak-plus) with attribution header; do not port its writer/conversion logic.

## Contract
```python
@dataclass(frozen=True)
class SignalHeader: label: str; unit: str; phys_min: float; phys_max: float; dig_min: int; dig_max: int
                    samples_per_record: int; rate_hz: float; prefiltering: str; transducer: str

@dataclass(frozen=True)
class Annotation: onset_s: float; duration_s: float | None; text: str; start: datetime  # naive local

class EdfFile:
    path: Path; start: datetime  # naive, as stored
    record_duration_s: float; n_records: int  # actual complete records
    is_edf_plus: bool; signals: dict[str, SignalHeader]; warnings: list[str]
    @classmethod
    def open(cls, path: Path) -> "EdfFile": ...
    def read_signal(self, label: str) -> np.ndarray: ...      # float32 physical values
    def annotations(self) -> list[Annotation]: ...            # [] for non-EDF+
```

## Out of scope
Mapping labels to canonical channels, units conversion, timezone handling (WP-03).

## Files
`src/pap_analytics/edf/__init__.py`, `edf/reader.py`, `edf/crc.py`, `tests/test_edf_reader.py`, `tests/edf_synth.py` (small synthetic EDF writer for edge cases only).

## Acceptance (real data, `tests/data/resmed-air11-autoset`)
- All 232 `.edf` files open without exception; zero unexpected warnings except documented truncations.
- `20261108_233938_PLD.edf`: 12 signals (Crc16 excluded) with labels/units exactly as in `tests/data/README.md`; rate 0.5 Hz; record duration 60 s.
- BRP: `Flow.40ms` rate 25 Hz, values within physical min/max.
- SA2 on 20261108: valid SpO2 (50–100) seconds = 22549 of 22860, min 89 (matches README).
- EVE annotations over whole set: Hypopnea 133, Apnea 20, Central Apnea 1, Arousal 1, Recording starts 46; timekeeping TALs not returned as events; no stray `+0`/garbage texts.
- `STR.edf` (both sets): opens; record count 1 resp. 10.
- Synthetic: truncated last record, `n_records=-1`, two-digit-year boundary, multi-text TAL.
