# Implementation plan

The plan is split into **work packages (WP)**. Each WP is self-contained: an agent reads `AGENTS.md`, `docs/ARCHITECTURE.md` and **one** `WP-*.md` and can deliver it. Status lives in GitHub issues (one issue per WP), never here.

## Goal (v1)

An agent connected to the MCP server answers, from real ResMed data incl. oximetry:
- "How was last night?" (usage, AHI device vs. derived, events, leak, pressure, SpO2)
- "Analyse 02:10–02:40" (window stats, events, downsampled signals)
- "Compare last week with the week before", "What is unusual about the night of 8 Nov?"

The server runs locally (stdio/HTTP), in Docker, and is deployed on the VPS with data synced read-only from the AirCANnect share.

## Work packages and dependencies

| WP | Title | Depends on | Parallel with |
|---|---|---|---|
| [WP-01](WP-01-foundation.md) | Foundation: canonical model, config, adapter protocol, CLI skeleton | – | – |
| [WP-02](WP-02-edf-reader.md) | Generic EDF/EDF+ reader | WP-01 | WP-04 |
| [WP-03](WP-03-resmed-adapter.md) | ResMed adapter (Identification, STR, DATALOG, mapping) | WP-02 | WP-04 |
| [WP-04](WP-04-store.md) | Store: DuckDB catalog + Parquet signals | WP-01 | WP-02, WP-03 |
| [WP-05](WP-05-ingest.md) | Incremental ingest + `pap ingest` / `pap doctor` | WP-03, WP-04 | – |
| [WP-06](WP-06-analytics-core.md) | Analytics core: nights, segments, events, leaks, settings, compare | WP-05 | WP-07 |
| [WP-07](WP-07-oximetry.md) | Oximetry analytics (SpO2/pulse, ODI, T<90) | WP-05 | WP-06 |
| [WP-08](WP-08-mcp-server.md) | MCP server (tools, resources, prompt, stdio + HTTP) | WP-06, WP-07 | – |
| [WP-09](WP-09-anomalies-plots.md) | Anomaly detection + optional plots | WP-06, WP-07 (MCP part: WP-08) | WP-08 |
| [WP-10](WP-10-packaging-deploy.md) | Docker, VPS deployment, data sync, Hermes integration | WP-08 | WP-09 |
| [WP-11](WP-11-lowenstein-research.md) | Löwenstein adapter – feasibility research (post-v1) | WP-03 | anything |

```
WP-01 ─┬─ WP-02 ── WP-03 ─┐
       └─ WP-04 ──────────┴─ WP-05 ─┬─ WP-06 ─┬─ WP-08 ── WP-10
                                    └─ WP-07 ─┘   └─ WP-09
```

## Shared Definition of Done (every WP)

1. All items in the WP's **Acceptance** section are met and covered by tests (real data from `tests/data/` wherever the WP says so).
2. `uv run ruff check . && uv run pytest` passes locally and in CI; fixture checksums unchanged.
3. Public functions are type-annotated; module docstring states purpose and boundaries.
4. Contracts are implemented exactly as specified; any deviation is a contract change (see `AGENTS.md`).
5. Docs updated by the filter in `AGENTS.md`; no status/progress written into `docs/plan/`.
6. PR from own worktree branch, description lists: what, how verified (commands + key results), assumptions made.
7. Source data opened read-only; no secrets committed.

## Conventions used in WPs

- **Scope** – what to build. **Out of scope** – belongs elsewhere, do not build.
- **Files** – files the WP owns (may create/modify). Touching other files only for trivial wiring (imports, registration), mention it in the PR.
- **Contract** – interface other WPs rely on; implement exactly.
- **Acceptance** – verifiable checks; numbers come from `tests/data/README.md` (device values, decoded independently) or are stated how to obtain independently.
