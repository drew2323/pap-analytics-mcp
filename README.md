<div align="center">

# pap-analytics-mcp

**Ask your AI agent how you slept on CPAP – and get answers grounded in your own device data.**

[![CI](https://github.com/drew2323/pap-analytics-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/drew2323/pap-analytics-mcp/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/tag/drew2323/pap-analytics-mcp?label=release&sort=semver)](https://github.com/drew2323/pap-analytics-mcp/tags)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

An [MCP](https://modelcontextprotocol.io) server that turns PAP therapy data (CPAP / APAP / BiPAP / ASV) into
small, aggregate tools an AI agent can call. The agent gets usage, AHI, events, leak, pressure and SpO2 –
not megabytes of raw waveforms – so you can simply ask:

- *"How was last night?"*
- *"Which parts of the night had the most events, and what kind?"*
- *"Analyse the hypopnea at 3:12 – what happened right before it?"*
- *"Did my SpO2 drop after the events?"*
- *"Compare this week with last week."*

### 🎙️ Talk it through by voice

Add the server as an MCP connector to a voice assistant (**ChatGPT Voice**, or other voice apps with MCP
support such as Dots, Muse or Grok) and ask the same questions out loud instead of reading charts.
The assistant answers from your own device data.
Setup: run the [HTTP server](#connect-your-agent) and make it reachable for the app
(e.g. via a tunnel); see [client integration](docs/OPERATIONS.md#client-integration).

## Features

- 🌙 **Night summaries, time windows and period comparisons:** from a whole night down to single breaths, or one week against another.
- 🔎 **Anomaly finder** that points at event clusters, large leak, desaturations and pressure at maximum.
- 🫁 **Oximetry** (SpO2, pulse, T90, ODI) when an oximeter is connected.
- 🏷️ **Clear provenance:** every number says whether it came from the device or was computed here.
- 🔒 **Local and read-only:** source data is never modified, and everything is stored on your machine (DuckDB + Parquet).
- 🧩 **Device-agnostic core:** vendors plug in as adapters. ResMed is available today.

## Supported devices

| Vendor | Devices | Source |
|---|---|---|
| ResMed | **AirSense 11** (tested, also with [airbreak-plus](https://github.com/m-kozlowski/airbreak-plus)); other AirSense/AirCurve 10/11 with the same SD-card layout should work | SD card or a copy of it, e.g. the [AirCANnect](https://github.com/drew2323/aircannect) share |
| Löwenstein | planned | – |

The source directory is the SD-card root, which contains `Identification.*`, `STR.edf` and `DATALOG/`.

## Quick start

**1. Install** (needs [uv](https://docs.astral.sh/uv/)):

```sh
uv tool install "git+https://github.com/drew2323/pap-analytics-mcp@v0.1.4"
```

Add `--with matplotlib` if you also want PNG plots (`plot_segment`).

**2. Configure:** create `~/.config/pap-analytics/config.toml`:

```toml
[[sources]]
name = "my-airsense"
path = "/media/SDCARD"          # SD-card root or a copy of it
timezone = "Europe/Prague"      # the device's clock timezone
```

**3. Import and check:**

```sh
pap doctor /media/SDCARD        # optional: what was found on the card
pap ingest                      # import (incremental; re-run any time)
pap nights --days 7             # one row per night
```

No data at hand? In a checkout, `uv run pap ingest --source tests/data/resmed-air11-autoset` imports the bundled sample nights.

## Connect your agent

**Claude Desktop / Claude Code / any stdio MCP client:**

```json
{
  "mcpServers": {
    "pap": { "command": "pap", "args": ["serve", "--stdio", "--read-only"] }
  }
}
```

Use the full path (e.g. `~/.local/bin/pap`) if the client does not see your `PATH`.

**Over HTTP** (another machine, e.g. over a VPN). This always requires a bearer token:

```sh
PAP_HTTP_TOKEN=$(openssl rand -hex 32) pap serve --http --read-only    # 127.0.0.1:8765/mcp
claude mcp add --transport http pap http://127.0.0.1:8765/mcp --header "Authorization: Bearer $PAP_HTTP_TOKEN"
```

`--read-only` hides the admin `ingest` tool. Without it, the agent can trigger an import itself.

## MCP tools

Every tool returns compact JSON with units and provenance (`device` or `derived`). Dates are `YYYY-MM-DD`,
`last` or `yesterday`; times are local `HH:MM` within the night (a night runs from noon to noon).

| Tool | What it does |
|---|---|
| `list_devices` | Known devices, their data range and when data was last imported |
| `list_nights` | One row per night: usage, AHI (device and derived), events, leak and pressure p95, oximetry flag |
| `night_summary` | Full picture of one night: sessions, mask-off gaps, events per hour, metrics, settings in effect, SpO2 |
| `find_anomalies` | Ranked parts of a night worth a look (event clusters, large leak, desaturations, pressure at max, outliers vs. the last 14 nights), each with a reason and a suggested next call |
| `get_events` | Events in a time window, optionally filtered by type (obstructive, central, hypopnea, RERA, …) |
| `segment_stats` | Min/median/mean/p95/max of every channel plus the events inside a window, e.g. `02:00`–`02:30` |
| `get_signal` | One channel (flow, pressure, leak, resp. rate, tidal volume, SpO2, …) downsampled to ≤ 2000 points |
| `get_signals` | Several channels on one time grid at full resolution, for a close look at a short window (breath by breath) |
| `oximetry_summary` | SpO2 and pulse: mean, minimum, T90, ODI 3 %/4 %, hypoxic burden, list of desaturations, artefacts filtered out |
| `disturbance_summary` | Disturbances beyond AHI in a night or window: pulse response to events, rapid pulse rises and their breathing response, hypoxic burden, flow limitation, calm vs. restless breathing, inspiratory flow shape (NED, Glasgow Index; split by pressure support) (measured) and possible arousals / RERA candidates (inferred, kept separate), with timestamps |
| `disturbance_trend` | The same disturbance metrics as one comparable row per night, with means over the range |
| `compare_periods` | Average nightly metrics of two periods with the absolute and relative change; optionally the nightly disturbance and flow-shape metrics too (e.g. before/after a pressure change) |
| `get_settings` | Device settings in effect on a date, or the full history of changes |
| `plot_segment` | PNG chart of a window with events shaded (needs `matplotlib`) |
| `ingest` | Admin: import new data now (hidden with `--read-only`) |

Also available: the resources `pap://glossary` (channels, units, event types) and `pap://devices`, and the prompt
`nightly_review` (a guided review of one night).
The same queries work on the command line: `pap nights`, `pap night last`, `pap segment last 02:10 02:40`.

## Configuration

Everything lives in one TOML file: `~/.config/pap-analytics/config.toml`, `$PAP_CONFIG` or `--config FILE`.
The full annotated example is [`config.example.toml`](config.example.toml).

| Key | Default | Meaning |
|---|---|---|
| `[[sources]] path` | – (required) | Directory to read; repeat `[[sources]]` for more devices |
| `[[sources]] name` | – | Short label (required when using `sync`) |
| `[[sources]] timezone` | `Europe/Prague` | Timezone of the device clock |
| `[[sources]] adapter` | `auto` | Force a vendor adapter, e.g. `resmed` |
| `store_dir` | `~/.local/share/pap-analytics` | Where imported data lives (disposable, rebuildable) |
| `day_boundary_hour` | `12` | A "night" runs from noon to noon, like in ResMed/OSCAR |
| `[http] host / port / token_file` | `127.0.0.1` / `8765` / – | HTTP transport |
| `[thresholds] <key>` | built-in defaults | Optional analytics overrides (leak, clusters, pressure, oximetry, outliers); every supported key, default, unit and range is listed in [`config.example.toml`](config.example.toml) |
| `[sources.sync]` | – | Optional rsync pull of the data from another host |

Environment overrides: `PAP_CONFIG`, `PAP_STORE_DIR`, `PAP_DAY_BOUNDARY_HOUR`, `PAP_HTTP_HOST`, `PAP_HTTP_PORT`,
`PAP_HTTP_TOKEN`, `PAP_HTTP_TOKEN_FILE`. Run `pap config show` to print the effective configuration (secrets redacted).

**Fresh data automatically:** run `pap ingest --watch` or use the systemd timer in [`deploy/systemd/`](deploy/systemd).
To pull the data from another machine, see [`[sources.sync]` in OPERATIONS](docs/OPERATIONS.md#sync-data-source).

## Docker

```sh
cp compose.example.yaml compose.yaml   # + config.toml with Docker paths, token in ./secrets/
docker compose up -d                   # ingest (watch) + MCP over HTTP on 127.0.0.1:8765
```

Details, secrets and the stdio fallback: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Under the hood

Not just a thin wrapper around files – the server has its own analytics layer:

- **Ingest:** own EDF/EDF+ reader and vendor adapters normalise the data into one vendor-neutral model; imports are incremental.
- **Store:** DuckDB catalog plus per-channel Parquet signals, rebuildable from the source at any time.
- **Analytics:** window statistics, event clustering, anomaly ranking, SpO2 (T90, ODI, desaturations, hypoxic burden), disturbances beyond AHI (pulse responses, flow limitation, breath-by-breath breathing stability and inspiratory flow shape, possible arousals), period comparison, settings history.
- **Interface:** 15 MCP tools, resources and a prompt over stdio or HTTP (bearer token) – for desktop agents, coding agents and voice assistants.

Python 3.12, NumPy, PyArrow, the official MCP SDK; matplotlib optional. Details in [ARCHITECTURE](docs/ARCHITECTURE.md).

## Development

```sh
uv sync
uv run pytest && uv run ruff check .
```

The tests run against real ResMed nights published as fixtures in [`tests/data/`](tests/data/README.md).
Before contributing, read [`AGENTS.md`](AGENTS.md) (repository rules) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

[`CLAUDE.md`](CLAUDE.md) points Claude Code to `AGENTS.md`, which is the single source of repository rules. Work is tracked as GitHub issues: one issue = one PR (body contains `Closes #<n>`), developed in its own git worktree, merged after the local quality gate and CI are green. Details are in the Workflow and Delivery sections of `AGENTS.md`.

| Doc | Content |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Components, canonical data model, contracts |
| [OPERATIONS](docs/OPERATIONS.md) | Runtimes, config reference, sync, deploy, troubleshooting |
| [DECISIONS](docs/DECISIONS.md) | Architecture decision log |
| [plan/](docs/plan/README.md) | Implementation work packages |

## License

[MIT](LICENSE). Parts of the EDF handling are derived from [airbreak-plus](https://github.com/m-kozlowski/airbreak-plus) (MIT).
