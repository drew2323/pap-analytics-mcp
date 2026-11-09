# WP-09 – Anomaly detection + optional plots

**Depends on:** WP-06, WP-07 (MCP registration after WP-08) · **Parallel with:** WP-08, WP-10

## Goal
Point the agent to the parts of a night worth looking at, and optionally render a picture of a segment.

## Scope
1. `analytics/anomalies.py` – `find_anomalies(store, device_id, day, thresholds=None) -> list[Anomaly]`, rule-based, each with `kind`, `start`, `end`, `severity` (0–1), `reason` (plain English, numbers included), `suggested_call` (e.g. `segment_stats(date, start, end)`):
   - event clusters (≥ 3 apnea/hypopnea within 10 min),
   - large leak spans (leak > threshold ≥ 2 min),
   - desaturation clusters / SpO2 < 90 % spans (from WP-07),
   - pressure at device max for ≥ 5 min,
   - minute ventilation far below target ventilation (ASV only, when `target_vent` present),
   - mask-off gaps > 10 min in the middle of the night,
   - nightly metric outliers vs the last 14 nights (z-score > 2 on AHI, leak p95, usage, ODI).
   Ranked by severity, capped at 20; thresholds in `config.thresholds` with defaults defined here.
2. `analytics/plot.py` (optional extra `pap-analytics-mcp[plot]`, `matplotlib`): `plot_segment(store, device_id, start, end, channels) -> bytes` (PNG, ≤ 200 kB, events as vertical bands, SpO2 on secondary axis).
3. MCP: `register_tools(server)` adding `find_anomalies(date)` and, if matplotlib installed, `plot_segment(date, start, end, channels?)` returning MCP image content.

## Contract
```python
@dataclass class Anomaly: kind: str; start: datetime; end: datetime; severity: float
                          reason: str; metrics: dict[str, Metric]; suggested_call: str
```

## Out of scope
Machine learning, own event scoring (ADR-005).

## Files
`src/pap_analytics/analytics/{anomalies,plot}.py`, `src/pap_analytics/mcp_extras.py`, `tests/test_anomalies.py`, `tests/test_plot.py`.

## Acceptance
- Synthetic store with injected leak span, event cluster and desat cluster → all three found with correct windows; clean synthetic night → `[]`.
- Real 2026-11-08: result deterministic, every anomaly window lies inside the night, the 89 % SpO2 minimum lies in a reported SpO2 anomaly or a note explains why not.
- `plot_segment` returns a valid PNG < 200 kB; tool absent when matplotlib missing (test via monkeypatch).
