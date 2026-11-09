# WP-10 – Packaging, VPS deployment, data sync, Hermes integration

**Depends on:** WP-08 · **Parallel with:** WP-09
**Infrastructure change – needs David's confirmation in the issue before touching servers.**

## Goal
The server runs durably on the VPS (`vpsagentfarma`), sees fresh data after each night, and Hermes (profile `david`) can query it.

## Context (verified at bootstrap)
- Source: AirCANnect SMB share on `hnizdo` at `/srv/aircannect`, readable only with `sudo -n` by user `david`; host reachable over Tailscale `100.107.233.0`, SSH key `~/.ssh/hag2_hermes`.

## Scope
1. `Dockerfile`: python 3.12-slim, `uv sync --frozen --no-dev`, non-root user, `ENTRYPOINT ["pap"]`, default `serve --http --host 0.0.0.0`, volumes `/data/source` (ro) and `/data/store`, `HEALTHCHECK` on `/healthz`.
2. `compose.example.yaml`: two services sharing the store volume – `ingest` (`pap ingest --watch 900`) and `mcp` (`pap serve --http --read-only`); source bind-mounted `:ro`; token via Docker secret/file.
3. **Sync from hnizdo to VPS** (decide in issue, default proposal): systemd timer on VPS running `rsync -a --delete-excluded` over SSH from `hnizdo:/srv/aircannect/` → `/srv/pap-analytics/source/` (read-only for the containers). Requires a read-only path for the rsync user on hnizdo (e.g. group/ACL instead of sudo) – exact access change to be confirmed by David.
4. Deployment on VPS: Docker Compose or native `uv` + systemd user units (choose by what the VPS already uses for MCP servers; check before deciding). HTTP bound to the Tailscale/loopback interface only, never public.
5. Hermes integration: register the MCP server in profile `david` (`mcp_servers` with HTTP URL + bearer token from a credential file, or stdio when co-located); verify with a real conversation question.
6. Create `docs/OPERATIONS.md` (hosts, paths, sync, deploy/update/rollback, backup of store = rebuildable from source, log locations) and one line in `~/Brain/systems/` catalog per Brain rules (outside the repo).

## Out of scope
Public internet exposure, multi-user auth, CI deployment pipelines.

## Files
`Dockerfile`, `.dockerignore`, `compose.example.yaml`, `deploy/` (systemd units, sync script), `docs/OPERATIONS.md`, README run sections.

## Acceptance
- `docker build` succeeds; `docker compose -f compose.example.yaml up` with `tests/data/resmed-air11-autoset` as source → `/healthz` 200 and tool call works with token (scripted smoke test `deploy/smoke.sh`).
- Local non-Docker run documented and smoke-tested (`uv run pap ingest && uv run pap serve --stdio`).
- On VPS: after the next night, `list_nights` shows the new day without manual action; service survives reboot.
- Hermes `david` profile answers "jaká byla poslední noc?" via the MCP tools.
- `docs/OPERATIONS.md` exists; access changes on hnizdo documented there.
