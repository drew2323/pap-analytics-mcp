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

## ADR-005 – No own respiratory event scoring in v1 (superseded by ADR-011)
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

## ADR-011 – Own signal analyses allowed when they add clear insight (supersedes ADR-005)
ADR-005 excluded any own analysis of the respiratory signals; in practice that blocked useful insight (#23: half of the pulse rises unexplained by device events show a recovery breath or reduced breathing). New rule: own analyses, including breath-by-breath analysis of the 25 Hz flow, are allowed when they demonstrably add insight on real data. Constraints: results are `derived`, kept apart from device values, `measured` vs `inferred` stays explicit, coverage is reported when the source signal is incomplete, and device events remain the AHI reference (no own AHI). Each analysis is validated on the fixture data before it is added; e.g. inspiratory-shape flattening and leak jumps did not separate pulse rises from controls and were not added. Sleep staging is not done: without EEG or a reference night it cannot be validated; breathing is described as calm/restless only.

## ADR-012 – Flow shape from AirwayLab methods, kept only where validated
Issue #27 evaluated the AirwayLab flow engines (NED/RERA, Glasgow Index, WAT) instead of self-hosting AirwayLab (browser-only, no automatic import). Ported on top of `segment_breaths`, with flow in L/min for the upstream thresholds, and validated on the fixture nights against three references: pulse rises vs random controls, the 30 s before device hypopneas vs controls, and nightly device AHI. Kept: NED (mean and flow-limited share: AUC 0.86 before hypopneas, nightly Spearman 0.80 with AHI), the Glasgow Index (AUC 0.71 for pulse rises, 0.69 before hypopneas) and its components skew, spike, multi breath and variable amplitude (each separates rises or hypopneas, AUC 0.55–0.95). The other five components (flat top, top heavy, multi peak, no pause, inspiration rate) do not separate in the expected direction or not at all; they stay inside the index only, since it is the published method, and are not reported individually. NED RERA candidates: upstream accepts runs with a rising NED or a max NED > 34 % without a sigh; those were followed by a pulse rise no more often than random times (7.6 % vs 7.4 %), so a sigh (next breath peak >= 1.5 × run mean) is required (50 % followed by a pulse rise). They are merged with the device-flow-limitation RERA candidates of ADR-010 (one list with `sources`), not a second list. Rejected: WAT (its FL score is the mid-inspiration flatness already rejected in ADR-011; its regularity and periodicity duplicate calm/restless breathing), the upstream estimated arousal index (duplicates recovery-breath links of pulse rises), the amplitude hypopnea detector (own event scoring, ADR-011) and the M-shape share (lower before hypopneas than at controls). Shape metrics are reported with pressure-support context because ASV pressure support reshapes inspiratory flow.
