# Operations

How `pap` runs in production and how to operate it. Read this before changing runtime, `Dockerfile`
or deployment; update it in the same PR when environments, hosts, sync or deploy procedure change.

## Runtime modes

One tool, one config schema, two equal runtimes. Docker only maps paths and secrets through mounts
and `PAP_*` environment variables; there is no Docker-specific logic in the code.

| | Native (production default) | Docker |
|---|---|---|
| Install | `uv tool install "git+https://github.com/drew2323/pap-analytics-mcp@vX.Y.Z"` → `~/.local/bin/pap` | `docker build -t pap-analytics-mcp:vX.Y.Z .` (compose: `compose.example.yaml`) |
| Ingest | user unit `pap-ingest.timer` → `pap ingest` every 15 min | service `ingest`: `pap ingest --watch` |
| MCP consumers | **stdio**: `pap serve --stdio --read-only`, started by the client | **HTTP** `http://127.0.0.1:8765/mcp` + bearer token |
| stdio fallback | – | `docker compose exec -T mcp pap serve --stdio --read-only` |
| Config | `$PAP_CONFIG` (e.g. `~/.config/pap-analytics/config.toml`) | `/etc/pap-analytics/config.toml` mounted, `PAP_CONFIG` set |
| Switching | change the runtime, not the config schema | |

Verify either runtime with `deploy/smoke.sh native` / `deploy/smoke.sh docker` (uses the committed
fixture `tests/data/resmed-air11-autoset`, no sync, no real secrets).

## Config reference

See `config.example.toml`. Secrets are referenced by file path, never inlined.

| Key | Meaning | Docker path |
|---|---|---|
| `store_dir` | disposable store (rebuildable from the source) | `/data/store` |
| `[[sources]] name` | label used by `--source`, `list_devices` (required with `sync`) | |
| `[[sources]] path` | local directory `ingest` reads | `/data/source/<name>` |
| `[[sources]] adapter`, `timezone` | `auto`, `Europe/Prague` | |
| `[sources.sync] type` | only `rsync` | |
| `[sources.sync] remote` | e.g. `pap-sync@hnizdo:/srv/aircannect/` | |
| `[sources.sync] ssh_key_file`, `known_hosts_file` | key (chmod 600) and pinned host key | `/run/secrets/pap_sync_key`, `/etc/pap-analytics/known_hosts` |
| `[sources.sync] interval_s` | used by `ingest --watch` (default 900, min 10) | |
| `[http] host, port, token_file` | HTTP transport; token required | `/run/secrets/pap_http_token` |

Env overrides: `PAP_CONFIG`, `PAP_STORE_DIR`, `PAP_DAY_BOUNDARY_HOUR`, `PAP_HTTP_HOST`, `PAP_HTTP_PORT`,
`PAP_HTTP_TOKEN_FILE`. `pap config show` prints the effective config; for key, known_hosts and token
files it shows only whether they exist and are readable.

## Sync (data source)

`pap sync [--source NAME]` pulls `remote` into `path` with `rsync -a --delete` over SSH
(`IdentitiesOnly`, `StrictHostKeyChecking=yes`, `BatchMode`, `--timeout=60`). The new tree is built beside
the current one (hard-linked, so only changes are transferred) and swapped in with two renames, so
`ingest` never sees a half-synced tree. Nothing is ever written to the remote. A failed sync keeps the
last good copy; `pap ingest` then still runs on it, prints the data age and exits 1.
Status is stored in `<store_dir>/sync_state.json` and surfaced by the MCP tool `list_devices`
(`sources[].last_sync_at`, `last_sync_ok`, `last_sync_error`, `last_ingest_at`, `newest_therapy_day`).

`pap ingest` syncs first (skip with `--no-sync`); `--watch [SECONDS]` loops sync + ingest.

### Concurrency
The writer takes the store lock only while ingesting (not during the rsync). `serve` opens the store
per request; on a lock conflict it retries 5 × 200 ms and then returns the tool error
"data is being updated, retry in a few seconds". The writer waits up to ~20 s for readers to release the file.

## Hosts and access

| Host | Role |
|---|---|
| `hnizdo` | holds the AirCANnect data at `/srv/aircannect` (directory `770 aircannect:aircannect`); source only, nothing is deployed there |
| `vpsagentfarma` | runs `pap` (ingest/sync + MCP), consumed by Hermes profile `david` and ChatGPT Voice via tunnel-client |

### Source host access (hnizdo)
- Dedicated user `pap-sync`, member of group `aircannect`; no sudo, no other paths.
- Dedicated ED25519 key (generated on vpsagentfarma, private key never leaves it, `chmod 600`).
- `~pap-sync/.ssh/authorized_keys` line:
  `command="rrsync -ro /srv/aircannect",restrict ssh-ed25519 AAAA… pap-sync@vpsagentfarma`
- Reachable only via Tailscale. Pin the host key: `ssh-keyscan -t ed25519 hnizdo > known_hosts`
  and verify the fingerprint out of band.
- The `remote` then reads `pap-sync@hnizdo:/` (rrsync root is `/srv/aircannect`; with a different rrsync
  layout use `pap-sync@hnizdo:/srv/aircannect/`).

## Install, update, rollback

Native:
1. Install a **released tag**, never the development checkout:
   `uv tool install --force "git+https://github.com/drew2323/pap-analytics-mcp@vX.Y.Z"`.
2. Config and secrets (`chmod 600`), then `pap config show`.
3. `pap ingest` (first full sync + import); install the units from `deploy/systemd/` into
   `~/.config/systemd/user/`, `systemctl --user enable --now pap-ingest.timer`
   (user lingering must be on to survive logout/reboot: `loginctl enable-linger $USER`).
4. Update = repeat 1; rollback = `uv tool install --force …@vPREV`. A `SCHEMA_VERSION` change makes
   the store refuse to open: run `pap ingest --rebuild` (the store is disposable).

Docker: `docker build -t pap-analytics-mcp:vX.Y.Z .`, set `PAP_VERSION`, `docker compose up -d`.
Rollback = previous image tag.

## Store rebuild, logs, troubleshooting

- Rebuild: `pap ingest --rebuild` (wipes only `store_dir` catalog and signals).
- Logs: native `journalctl --user -u pap-ingest.service`; Docker `docker compose logs ingest mcp`.
- **Stale data** ("data are N days old"): `list_devices` → `sources[]`. `last_sync_ok=false` → read
  `last_sync_error`; `last_sync_ok=true` but old `newest_therapy_day` → the source itself has no new night.
- **Sync auth failure** (`Permission denied`, `Host key verification failed`): check key permissions
  (`pap config show`), the `authorized_keys` line on hnizdo and `known_hosts`. Ingest keeps serving the last copy.
- **Lock conflicts**: tool error "data is being updated" is transient; persistent → a stuck `pap ingest`
  (`ps`, `store.lock` in `store_dir`).
- **Schema mismatch**: `pap ingest --rebuild`.

## Client integration

Use a separate HTTP token per client so either can be revoked. One `pap serve` process has one token, so run one
server instance (own port, own `PAP_HTTP_TOKEN_FILE`) per HTTP client. Stdio clients need no token.

| Client | Native | Docker |
|---|---|---|
| Hermes `david` (`mcp_servers`) | `command: pap`, `args: [serve, --stdio, --read-only]`, `env: {PAP_CONFIG: …}` | `url: http://127.0.0.1:8765/mcp` + `headers: {Authorization: Bearer <token from token file>}` |
| ChatGPT Voice (`tunnel-client`, profile `~/.config/tunnel-client/pap-david.yaml`, user unit `pap-chatgpt-tunnel.service` modelled on `hermes-chatgpt-tunnel.service`) | `mcp.commands: [{channel: main, command: <launcher>}]` with `deploy/launchers/pap-mcp-stdio` | `--mcp.server-url url=http://127.0.0.1:8765/mcp,channel=main` + `--mcp.extra-headers 'Authorization: Bearer …'` (**unverified – test first**; fallback: the stdio launcher with `PAP_RUNTIME=docker`) |

## Production setup log

Live setup on `vpsagentfarma` (native mode, release `v0.1.1`):

- hnizdo: user `pap-sync` (system, locked password, group `aircannect`), `rsync` installed (Debian
  `rsync` package provides `/usr/bin/rrsync`), `authorized_keys`:
  `command="/usr/bin/rrsync -ro /srv/aircannect",restrict ssh-ed25519 …`. `remote = pap-sync@100.107.233.0:/`.
  The private key `~/.config/pap-analytics/sync_ed25519` exists only on vpsagentfarma; pinned
  host key in `known_hosts` (ED25519 fingerprint checked against `ssh-keygen -lf` on hnizdo).
- Checked: pulling works; writing (`rsync` upload) and arbitrary commands are refused by rrsync.
- vpsagentfarma: `uv tool install "git+https://github.com/drew2323/pap-analytics-mcp@v0.1.1" --with matplotlib`;
  config `~/.config/pap-analytics/config.toml` (store `~/.local/share/pap-analytics/store`, source copy
  `~/.local/share/pap-analytics/source/aircannect`); units `pap-ingest.{service,timer}` in
  `~/.config/systemd/user/`, user linger on.
- Hermes `david`: `mcp_servers.pap` (stdio, `PAP_CONFIG` env), config backup `config.yaml.bak-pap-*`.
  Hermes only offers MCP tools it has configured; "jaká byla poslední noc?" is answered from pap tools.
- ChatGPT Voice: user unit `pap-chatgpt-tunnel.service` (modelled on `hermes-chatgpt-tunnel.service`) runs
  `tunnel-client run --profile pap-david` with profile `~/.config/tunnel-client/pap-david.yaml` (own tunnel,
  `mcp.commands` → `~/.local/bin/pap-mcp-stdio`) and runtime key file `openai-runtime-key-pap` (0600). Outbound only.
  Check: `curl "$(cat ~/.local/state/pap-chatgpt/health.url)/readyz"` → `ready`. The runtime key is not stored in the repo.

Update: bump the tag and `uv tool install --force …@vX.Y.Z --with matplotlib`; rollback with the previous tag.
