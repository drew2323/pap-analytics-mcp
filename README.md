# pap-analytics-mcp

Device-agnostic analytics over PAP (CPAP/APAP/BiPAP/ASV) therapy data, exposed to AI agents as an **MCP server**.

Ask an agent *"How was last night?"*, *"Analyse 02:10–02:40"*, *"Compare this week with last week"* – the agent calls small, aggregate-first tools instead of reading raw waveforms.

```
raw device data ──► vendor adapter ──► canonical PAP model ──► store ──► analytics ──► MCP tools
(ResMed SD card /      (ResMed now,       (vendor-neutral)      (DuckDB +              (stdio / HTTP)
 AirCANnect SMB share)  Löwenstein later)                         Parquet)
```

- **Sources (v1):** ResMed AirSense/AirCurve 10/11 SD-card layout (`Identification.*`, `STR.edf`, `DATALOG/`). The [AirCANnect](https://github.com/drew2323/aircannect) SMB share uses the same layout, so a real SD card and the share both work.
- **Later:** Löwenstein (prisma) and other vendors through the same adapter contract.
- **Runs:** locally (`uv`) or in Docker, read-only over the source data.
- **Not a medical device.** Output is analytical, not a diagnosis.

## Status

Scaffold only. Implementation follows the work packages in [`docs/plan/`](docs/plan/README.md); progress is tracked in Multica (project *PAP Analytics MCP*).

## Quick start (development)

```sh
uv sync
uv run pytest          # quality gate (also run in CI)
uv run ruff check .
uv run pap --help
```

Real test data (the author's own nights, published on purpose) lives in [`tests/data/`](tests/data/README.md).

## Documentation

| File | Content |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Rules for humans and coding agents working in this repo |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Stable shape: components, canonical model, contracts, invariants |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Append-only decision log (ADR) |
| [`docs/plan/`](docs/plan/README.md) | Implementation plan split into self-contained work packages |
| [`tests/data/README.md`](tests/data/README.md) | Real-data fixture description and known facts |

## License

MIT – see [LICENSE](LICENSE). Parts of the EDF handling are derived from [airbreak-plus](https://github.com/m-kozlowski/airbreak-plus) (MIT).
