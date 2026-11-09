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
small, aggregate tools an AI agent can call. Your agent asks *"How was last night?"* and gets usage, AHI,
events, leak, pressure and SpO2 – not megabytes of raw waveforms.

```text
You:    How was last night? Anything unusual?
Agent:  → night_summary("last") → find_anomalies("last") → segment_stats("last", "05:15", "05:22")
        6 h 20 min of use, AHI 1.0 (device), leak low. One cluster of 6 small desaturations
        between 05:16 and 05:21 (lowest SpO2 89 %); the rest of the night stayed around 97 %.
```

> [!WARNING]
> **Not a medical device.** The output is analytical, not a diagnosis. Talk to your clinician about therapy changes.

## Features

- 🌙 **Night summaries, time windows and period comparisons:** *"Analyse 02:10–02:40"*, *"This week vs. last week"*.
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

## What the agent gets

| Tool | Answers |
|---|---|
| `list_devices`, `list_nights` | Which devices, which nights, usage and AHI at a glance |
| `night_summary` | One night: sessions, mask-off gaps, events, device vs. derived metrics, settings |
| `get_events`, `segment_stats` | Events and statistics within a time window (`02:10`–`02:40`) |
| `get_signal`, `get_signals` | Downsampled channels (flow, pressure, leak, SpO2, …), never more than 2000 points |
| `find_anomalies` | Parts of a night worth a look, ranked |
| `oximetry_summary` | SpO2/pulse metrics with artefact filtering |
| `compare_periods` | Period A vs. B with absolute and relative change |
| `get_settings` | Device settings in effect on a date, or their history |
| `plot_segment` | PNG plot of a segment (needs `matplotlib`) |

Plus the resources `pap://glossary` and `pap://devices`, and the prompt `nightly_review`.
The same queries are available on the command line: `pap nights`, `pap night last`, `pap segment last 02:10 02:40`.

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

## Development

```sh
uv sync
uv run pytest && uv run ruff check .
```

The tests run against real ResMed nights published as fixtures in [`tests/data/`](tests/data/README.md).
Before contributing, read [`AGENTS.md`](AGENTS.md) (repository rules) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

| Doc | Content |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Components, canonical data model, contracts |
| [OPERATIONS](docs/OPERATIONS.md) | Runtimes, config reference, sync, deploy, troubleshooting |
| [DECISIONS](docs/DECISIONS.md) | Architecture decision log |
| [plan/](docs/plan/README.md) | Implementation work packages |

## License

[MIT](LICENSE). Parts of the EDF handling are derived from [airbreak-plus](https://github.com/m-kozlowski/airbreak-plus) (MIT).
