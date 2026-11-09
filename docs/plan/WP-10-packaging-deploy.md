# WP-10 – Packaging, VPS deployment, data sync, Hermes integration

**Depends on:** WP-08 · **Parallel with:** WP-09
**Infrastructure change – needs David's confirmation in the issue before touching servers.**

## Goal
The server runs durably on the VPS, sees fresh data after each night, and the Hermes agent can query it.

## Context (verified at bootstrap)
- Source: AirCANnect SMB share on a separate host in the tailnet at `/srv/aircannect`, readable only with sudo by the admin user. Concrete hosts and keys are kept outside the repository.

## Scope
1. `Dockerfile`: python 3.12-slim, `uv sync --frozen --no-dev` (optional `plot` extra), non-root user, `ENTRYPOINT ["pap"]`, default `serve --http --host 0.0.0.0`, volumes `/data/source` (ro) and `/data/store`, `HEALTHCHECK` on `/healthz`.
2. `compose.example.yaml`: two services sharing the store volume – `ingest` (`pap ingest --watch`) and `mcp` (`pap serve --http --read-only`); source and config mounted `:ro`; token via Docker secret/file.
3. **Config + sync (code):** `[[sources]]` gets `name` and an optional `[sources.sync]` block (`type = "rsync"`, `remote`, `ssh_key_file`, `known_hosts_file`, `interval_s`). `pap sync` / `pap ingest` (sync first, `--no-sync`, `--watch`) pull the remote into `path` via a temp sibling directory and atomic rename; pull only, strict pinned host key, last good copy kept on failure; status in `<store_dir>/sync_state.json`, exposed by `list_devices` as `sources[]`. `pap config show` prints the effective config with secrets redacted. Reader/writer lock contention is retried; readers then get a clear "data is being updated" tool error.
4. **Two equal runtimes over one config:** native (`uv tool install` from a release tag, ingest by a systemd user timer, MCP over stdio) – the production default on the VPS – and Docker (HTTP + bearer token, stdio fallback via `docker compose exec -T`). `deploy/smoke.sh native|docker` verifies each against the committed fixture. HTTP is bound to loopback/Tailscale only, never public.
5. **Access to the source host:** dedicated system user `pap-sync` in group `aircannect` with its own key restricted to `rrsync -ro /srv/aircannect` (`restrict`); private key stays on the VPS; host key pinned. No sudo, nothing writable.
6. **Clients:** Hermes via `mcp_servers.pap` (stdio, `--read-only`); ChatGPT Voice via a `tunnel-client` profile and a user unit running the stdio launcher `deploy/launchers/pap-mcp-stdio` (own tunnel and runtime key, never shared with another service).
7. `docs/OPERATIONS.md` (hosts, config reference, sync, concurrency, install/update/rollback, troubleshooting, client integration, production setup log) and one line in the `~/Brain/systems/` catalog per Brain rules (outside the repo).

## Out of scope
Public internet exposure, multi-user auth, CI deployment pipelines.

## Files
`Dockerfile`, `.dockerignore`, `compose.example.yaml`, `deploy/` (systemd units, launcher, smoke test), `src/pap_analytics/{sync,cli_sync,config}.py` and the wiring they need, `docs/OPERATIONS.md`, README run sections.

## Acceptance
- `docker build` succeeds; `docker compose -f compose.example.yaml up` with `tests/data/resmed-air11-autoset` as source → `/healthz` 200 and tool call works with token (scripted smoke test `deploy/smoke.sh`).
- Local non-Docker run documented and smoke-tested (`deploy/smoke.sh native`).
- Sync tests cover success, failure keeping the last copy, atomic swap, config validation and reader/writer contention.
- On VPS: after the next night, `list_nights` shows the new day without manual action; service survives reboot.
- Hermes answers "jaká byla poslední noc?" via the MCP tools.
- `docs/OPERATIONS.md` exists; access changes on the source host documented there.
