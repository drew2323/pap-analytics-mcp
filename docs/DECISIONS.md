# Decisions

Append-only. One entry per significant, hard-to-reverse choice. Next number = highest + 1 on `main`.

## ADR-001 – Standalone repo, not an extension of `edf_resscan.py`
`airbreak-plus/python/edf_resscan.py` is a ResMed EDF *converter/writer* (ASV → synthetic CPAP). Analytics needs a reader, normalisation and storage across vendors. We build a separate repo and port the relevant EDF/ResMed knowledge (MIT, attribution kept in file headers). airbreak-plus stays focused on device tooling.

## ADR-002 – Own vendor-neutral canonical model
Not EDF, not OSCAR's internal structures, not FHIR. A small model (`model.py`) with fixed canonical channel/event names and units, versioned by `SCHEMA_VERSION`. Vendors are mapped by adapters; analytics never sees vendor formats. Reason: Löwenstein/Philips data does not map onto EDF; analytics must be written once.

## ADR-003 – Store: DuckDB catalog + Parquet signals
Embedded, no server, columnar, fast window/percentile queries; one directory works the same in Docker and locally. SQLite was rejected (hand-written aggregations, slow on per-sample data); keeping only raw files was rejected (re-parsing on every query).

## ADR-004 – Aggregate-first MCP tools with provenance
Agents get summaries, window statistics and downsampled series (≤ 300 points by default), never raw 25 Hz data – context budget. Every metric is labelled `device` or `derived` so agent answers do not pass computed numbers off as device values.

## ADR-005 – No own respiratory event scoring in v1
Device-reported events are the reference. Derived analytics are limited to statistics, oximetry indices (ODI, T<90) and rule-based anomaly flags. Own scoring is a separate future decision.

## ADR-006 – Real personal data as committed test fixtures
The author publishes his own nights (`tests/data/resmed-air11-autoset/`) deliberately as test data. Tests run on real data in CI; the fixture is immutable. Synthetic EDFs only for edge cases.

## ADR-007 – Implementation plan lives in the repo as self-contained work packages
Exception to the usual "no plan in repo" convention: `docs/plan/WP-*.md` are specifications (scope, contracts, acceptance), so an agent can take exactly one package. Status/progress stays in Multica issues; the plan files contain no status.

## ADR-008 – License MIT, public repository
Compatible with airbreak-plus (MIT) code reuse; permissive for other PAP tooling.

## ADR-009 – Therapy mode is read from each session's data
The author's AirSense 11 runs modified firmware with all modes enabled and is switched between modes (e.g. CPAP 4.0 on 2026-11-03, ASV otherwise). Neither the device model nor STR settings (per day, often only a few records on the share) say what a given night ran in. The adapter therefore derives `Session.mode` from the session's recorded channels; analytics use it as the night's mode and flag settings of another mode as not applying. Alternative rejected: trust the latest STR `Mode` (wrong for every switched night).

## ADR-010 – Disturbance metrics beyond AHI: measured vs. inferred, no own scoring
Issue #21 asked for metrics that catch disturbances AHI misses. Available signals: device events, 0.5 Hz device flow limitation/tidal volume, and 1 Hz pulse/SpO2 on oximetry nights only (no EEG). We keep four metrics in adjusted form and split every result into `measured` (pulse response to device events, rapid pulse rises, desaturation-area hypoxic burden, flow-limitation episodes from the device channel) and `inferred` (possible autonomic arousals = pulse rises linked or not to a respiratory trigger, RERA candidates = flow limitation ended by a recovery breath outside device events). Inferred items are candidates, never enter AHI and are not stored (computed on demand), which keeps ADR-005 intact. Rejected: breath-by-breath flow-shape scoring from the 25 Hz waveform (that is own respiratory scoring and duplicates the device's flow-limitation estimate); the published event-window hypoxic burden (needs scored-event-based ensemble windows and longer recordings than many nights have); merging pulse rises into an "arousal index" without the measured/inferred split (would read as EEG arousals).
