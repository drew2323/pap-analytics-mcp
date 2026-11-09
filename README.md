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

Implemented: ResMed adapter, store, ingest, analytics, oximetry and the MCP server (WP-01…08). Anomaly detection, optional plots (WP-09), packaging, sync and deployment files (WP-10) are in. Plan: the work packages in [`docs/plan/`](docs/plan/README.md); progress is tracked in Multica (project *PAP Analytics MCP*).

## Quick start (development)

```sh
uv sync
uv run pytest          # quality gate (also run in CI)
uv run ruff check .
uv run pap --help
```

Real test data (the author's own nights, published on purpose) lives in [`tests/data/`](tests/data/README.md).

## Running the MCP server

```sh
uv run pap ingest --source /path/to/sd-card      # fill the store (read-only on the source)
uv run pap serve --stdio                          # local agents
PAP_HTTP_TOKEN=$(openssl rand -hex 32) uv run pap serve --http --port 8765   # needs a bearer token
```

HTTP binds `127.0.0.1`, refuses to start without a token (`PAP_HTTP_TOKEN` or `http.token_file`;
`--insecure-no-auth` only on loopback), serves MCP at `/mcp` and an unauthenticated `GET /healthz`.
`--read-only` hides the admin `ingest` tool. `plot_segment` needs the extra: `uv sync --extra plot`.

Claude Desktop / Claude Code (stdio):

```json
{"mcpServers": {"pap": {"command": "uv", "args": ["--directory", "/path/to/pap-analytics-mcp", "run", "pap", "serve", "--stdio"]}}}
```

Claude Code over HTTP: `claude mcp add --transport http pap http://host:8765/mcp --header "Authorization: Bearer $TOKEN"`

Hermes (`mcp_servers`):

```yaml
mcp_servers:
  pap:                       # stdio
    command: uv
    args: ["--directory", "/path/to/pap-analytics-mcp", "run", "pap", "serve", "--stdio"]
  pap_http:                  # or HTTP
    url: http://host:8765/mcp
    headers: {Authorization: "Bearer ${PAP_HTTP_TOKEN}"}
```

## Run locally

```sh
uv tool install "git+https://github.com/drew2323/pap-analytics-mcp@vX.Y.Z"   # or: uv sync in a checkout
cp config.example.toml ~/.config/pap-analytics/config.toml                  # set sources[].path
pap config show        # effective config, secrets redacted
pap ingest             # (sync first if [sources.sync] is configured), then import
pap serve --stdio --read-only
```

Optional `[sources.sync]` pulls the data from another host with rsync; see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md). `deploy/systemd/` has the ingest timer.

## Run in Docker

```sh
cp compose.example.yaml compose.yaml            # config.toml with the Docker paths, ./secrets/*
docker compose up -d                            # ingest (watch) + mcp on 127.0.0.1:8765
docker compose exec -T mcp pap serve --stdio --read-only   # stdio fallback
deploy/smoke.sh docker                          # self-test against the fixture
```

## Connect a client

Native clients start `pap serve --stdio --read-only` themselves (`deploy/launchers/pap-mcp-stdio`);
Docker clients use `http://127.0.0.1:8765/mcp` with a bearer token. Snippets for Hermes and the
ChatGPT tunnel are in [`docs/OPERATIONS.md`](docs/OPERATIONS.md#client-integration).

## Documentation

| File | Content |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Rules for humans and coding agents working in this repo |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Stable shape: components, canonical model, contracts, invariants |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Runtime modes, config, sync, deploy, troubleshooting |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Append-only decision log (ADR) |
| [`docs/plan/`](docs/plan/README.md) | Implementation plan split into self-contained work packages |
| [`tests/data/README.md`](tests/data/README.md) | Real-data fixture description and known facts |

## License

MIT – see [LICENSE](LICENSE). Parts of the EDF handling are derived from [airbreak-plus](https://github.com/m-kozlowski/airbreak-plus) (MIT).
