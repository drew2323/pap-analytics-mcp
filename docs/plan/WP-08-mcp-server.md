# WP-08 – MCP server

**Depends on:** WP-06, WP-07 · **Unblocks:** WP-10 (WP-09 adds tools)

## Goal
Expose analytics to agents via MCP, over stdio and streamable HTTP, with small typed tools.

## Scope
1. Dependency `mcp>=1.12,<2` (official SDK, FastMCP).
2. `src/pap_analytics/mcp_server.py` – `build_server(config) -> FastMCP`; opens the store read-only per request batch (re-open if the ingest log changed, so new data appears without restart).
3. Tools (names and parameters are contract; return = `to_dict()` of WP-06/07 results):

| Tool | Parameters | Notes |
|---|---|---|
| `list_devices` | – | devices, data range, last ingest time |
| `list_nights` | `date_from?`, `date_to?` (default last 14 days), `device_id?` | |
| `night_summary` | `date` (`YYYY-MM-DD` \| `last`), `device_id?` | includes oximetry |
| `get_events` | `date`, `start?`, `end?`, `types?: list[str]`, `limit=200` | |
| `segment_stats` | `date`, `start`, `end`, `channels?` | times `HH:MM` within therapy day or ISO |
| `get_signal` | `date`, `start`, `end`, `channel`, `max_points=300` | cap 2000 |
| `oximetry_summary` | `date`, `start?`, `end?` | |
| `disturbance_summary` | `date`, `start?`, `end?` | `to_dict(DisturbanceSummary)`: `measured` / `inferred` metric dicts (incl. breathing stability), `findings` (`pulse_rise` with `breathing_response`, `flow_limitation`, `rera_candidate`, `restless_breathing`), `event_responses` (`analytics/disturbances.py`) |
| `disturbance_trend` | `date_from?`, `date_to?` (default last 14 days) | `{metrics: {key: basis, unit, method, mean, nights}, nights: [{date, <metric>: value \| null}]}` |
| `compare_periods` | `a_from`, `a_to`, `b_from`, `b_to` | |
| `get_settings` | `date?` | settings in effect (+ `session_mode`, `applies: false` and a note when the night ran in another mode) / history |
| `ingest` | `source?` | admin; **not registered** with `--read-only` |

   `device_id` optional everywhere: if exactly one device exists use it, else return an error listing devices.
4. Errors: `ToolError` with actionable text (unknown date → nearest available days; unknown channel → list of channels for that day).
5. Resources: `pap://glossary` (channels with units, event types, provenance meaning, therapy day definition, disclaimer "analytical, not a diagnosis"), `pap://devices`.
6. Prompt `nightly_review(date)` – instructs the agent to call `night_summary`, check `find_anomalies` if available, distinguish device vs derived values, and add the disclaimer.
7. CLI `pap serve [--stdio | --http] [--host] [--port] [--read-only]`; HTTP requires bearer token from `http.token_file` or `PAP_HTTP_TOKEN`, refuses to start without one unless `--insecure-no-auth` and host is loopback. Health endpoint `GET /healthz` (no auth, no data).
8. README section with client config snippets (Claude Desktop/Code, Hermes `mcp_servers` stdio + HTTP).

## Out of scope
Anomaly detection and plots (WP-09 registers its tools via `register_tools(server)`), deployment (WP-10).

## Files
`src/pap_analytics/mcp_server.py`, `src/pap_analytics/mcp_auth.py`, `src/pap_analytics/cli_serve.py`, `tests/test_mcp_tools.py`, `tests/test_mcp_http.py`, README section.

## Acceptance
- In-process MCP client (`mcp` SDK `ClientSession` over memory streams) against a store ingested from fixtures: `list_tools` returns exactly the table above (minus `ingest` with `--read-only`); every tool callable; JSON outputs validate against their declared schema.
- `night_summary(date="2026-11-08")` contains device AHI 1.0, two sessions, oximetry with `spo2_min`.
- `segment_stats(date="2026-11-08", start="02:00", end="02:30")` works with local times.
- `get_signal` response size < 40 kB at `max_points=300`.
- HTTP: request without/with wrong token → 401; correct token → tool list; `/healthz` 200.
- Manual check documented in PR: MCP Inspector or Hermes stdio session answers "jaká byla noc 8. 11.?" using only the tools.
