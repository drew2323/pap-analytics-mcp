# WP-06 – Analytics core

**Depends on:** WP-05 (store with real data) · **Unblocks:** WP-08, WP-09 · **Parallel with:** WP-07

## Goal
Pure analytics functions over the store that answer the v1 questions with compact, JSON-serialisable results carrying units and provenance.

## Scope
Modules in `src/pap_analytics/analytics/`:
1. `results.py` – result dataclasses (contract below) + `to_dict()` (stable key order, floats rounded to sensible precision, datetimes ISO local with offset).
2. `timeparse.py` – resolve user input: `date` (`YYYY-MM-DD`, `last`, `yesterday`), times `HH:MM` within the therapy day window (e.g. `02:10` on day 2026-11-08 → 2026-11-09 02:10 local), ISO datetimes.
3. `nights.py`
   - `list_nights(store, device_id, day_from, day_to)` – per day: usage (derived from sessions; device if available), sessions count, AHI device + derived (events/usage hours), event counts by type, leak p50/p95, pressure p50/p95, oximetry flag.
   - `night_summary(store, device_id, day)` – sessions with start/end, mask-off gaps, events by type and per hour, channel stats (p5/p50/p95/max) for `pressure`, `mask_pressure`, `leak`, `resp_rate`, `tidal_volume`, `minute_vent`, `target_vent`, `flow_limitation`, `snore`; device metrics side by side with derived; time with leak > threshold (default 0.4 L/s ≈ 24 L/min, config `thresholds.large_leak_lps`); settings in effect; oximetry summary hook (calls WP-07 if available, else `None`).
4. `segments.py`
   - `segment_stats(store, device_id, start, end, channels=None)` – per channel min/mean/p50/p95/max, coverage %, events inside the window, leak time over threshold.
   - `downsample(channel_data, max_points)` – bucket min/mean/max, equal-time buckets, NaN-aware; returns `SeriesResult`.
   - `get_signal(store, device_id, start, end, channel, max_points=300)`; hard cap 2000 points; `flow`/`pressure_hr` allowed only for windows ≤ 30 min (else error suggesting a shorter window).
5. `events.py` – `get_events(store, device_id, start, end, types=None, limit=200)`; `event_clusters(events, min_events=3, window_min=10)`.
6. `compare.py` – `compare_periods(a, b)` → means of key nightly metrics per period + absolute/relative delta.
7. `settings.py` – `settings_history(store, device_id)` with change points.

## Contract (results)
```python
@dataclass class StatBlock: unit: str; min: float|None; p5: float|None; p50: float|None; mean: float|None
                            p95: float|None; max: float|None; coverage_pct: float; provenance: str = "derived"
@dataclass class NightRow: date: date; usage_min: Metric; sessions: int; ahi_device: Metric|None
                           ahi_derived: Metric; events: dict[str,int]; leak_p95: Metric|None
                           pressure_p95: Metric|None; has_oximetry: bool
@dataclass class NightSummary: date: date; device_id: str; sessions: list[dict]; mask_off_gaps: list[dict]
                               events_by_type: dict[str,int]; events_per_hour: list[dict]
                               device_metrics: dict[str,Metric]; derived_metrics: dict[str,Metric]
                               channels: dict[str,StatBlock]; settings: dict|None; oximetry: dict|None
                               notes: list[str]
@dataclass class SegmentStats: start: datetime; end: datetime; channels: dict[str,StatBlock]
                               events: list[dict]; notes: list[str]
@dataclass class SeriesResult: channel: str; unit: str; start: datetime; bucket_s: float
                               t: list[float]; min: list[float|None]; mean: list[float|None]; max: list[float|None]
```
`notes` explain gaps/assumptions in plain English (e.g. "STR summary not available for this day; device metrics missing").

## Out of scope
Oximetry metrics (WP-07), anomaly ranking and plots (WP-09), MCP wiring (WP-08).

## Files
`src/pap_analytics/analytics/{__init__,results,timeparse,nights,segments,events,compare,settings}.py`, `src/pap_analytics/cli_query.py` (`pap nights`, `pap night`, `pap segment` with `--json`), `tests/test_analytics_*.py`.

## Acceptance (store ingested from fixtures)
- `night_summary(2026-11-08)`: 2 sessions; device AHI 1.0 and usage 383 min reported as `device`; derived AHI = (EVE apnea+hypopnea events on that day) / usage h, computed independently in the test from raw annotation counts.
- `list_nights(2026-10-16..25)` after ingesting the STR history: device AHI equals `tests/data/README.md` table for every day; derived usage within ±2 min of STR `Duration` for normal nights; deviation on 2026-10-18 surfaces as a note.
- `segment_stats(2026-11-08, 02:00, 02:30)`: coverage 100 % for PLD channels; events inside window match a manual filter.
- `downsample`: 8 h at 0.5 Hz → 300 buckets; min ≤ mean ≤ max; NaN-only bucket → None.
- `get_signal(flow, 2 h window)` rejected with helpful message.
- `compare_periods` on two synthetic periods returns exact deltas.
