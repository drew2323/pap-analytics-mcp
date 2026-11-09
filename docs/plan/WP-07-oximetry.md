# WP-07 – Oximetry analytics

**Depends on:** WP-05 · **Unblocks:** WP-08, WP-09 · **Parallel with:** WP-06

## Goal
Derived oximetry metrics from the canonical `spo2`/`pulse` channels (ResMed SA2 today; any oximeter source later), robust to sensor artefacts.

## Scope
`src/pap_analytics/analytics/oximetry.py`:
1. **Artefact filter** (before every metric): values outside 50–100 % already NaN from adapter; additionally drop samples where SpO2 changes > 4 %/s (configurable `thresholds.spo2_max_slope`), and isolated valid runs < 10 s. Report `valid_s`, `recorded_s`, `valid_pct`.
2. **Baseline + desaturations:** baseline = max valid SpO2 over the preceding 120 s (configurable `baseline_window_s`); a desaturation = drop ≥ 3 % (ODI3) / ≥ 4 % (ODI4) below baseline lasting ≥ 10 s; event start/end/nadir/depth. Emit as canonical `desaturation` events (provenance `derived`, not stored – computed on demand).
3. **Metrics (all `derived`):** `spo2_mean`, `spo2_p50`, `spo2_min` (after artefact filter), `t90_min` + `t90_pct` (time < 90 %), `t88_min`, `odi3`, `odi4` (events/valid hour), `pulse_mean`, `pulse_min`, `pulse_max`, `pulse_p95`; association: share of desaturations that start within 30 s after a device respiratory event end.
4. `oximetry_summary(store, device_id, day | (start, end))` → `OximetryResult` (dict-compatible, per contract style of WP-06: `Metric` values + `notes` + desaturation list capped at 100).
5. Hook for WP-06: `night_summary` calls `oximetry_summary` when the day has `spo2`.

## Contract
```python
@dataclass class OximetryResult: start: datetime; end: datetime; recorded_s: float; valid_s: float
                                 metrics: dict[str, Metric]; desaturations: list[dict]; notes: list[str]
def oximetry_summary(store: Store, device_id: str, start: datetime, end: datetime,
                     thresholds: Mapping[str, float] | None = None) -> OximetryResult | None  # None = no spo2 data
def detect_desaturations(spo2: ChannelData, drop_pct: float = 3.0, min_duration_s: float = 10.0,
                         baseline_window_s: float = 120.0) -> list[Event]: ...
```

## Out of scope
Clinical interpretation; other oximeter file formats (separate adapter later).

## Files
`src/pap_analytics/analytics/oximetry.py`, `tests/test_oximetry.py`.

## Acceptance
- Synthetic series: known number of 3 %/4 % desats → exact ODI3/ODI4; artefact spikes removed; baseline window respected.
- Real 2026-11-08: `valid_s` = 22549, `spo2_min` = 89 before slope filter (document whether the slope filter changes it), `t90_min` > 0 small; ODI values plausible (< 15) and reproduced by an independent simple implementation inside the test (loop-based, no shared code).
- Real days without valid SpO2 (e.g. 2026-11-04) → `None`, and `night_summary.oximetry` is `None` with a note.
- Pulse metrics present when SpO2 is present.
