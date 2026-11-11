# Operations

How `pap` runs in production and how to operate it. Read this before changing runtime, `Dockerfile`
or deployment; update it in the same PR when environments, hosts, sync or deploy procedure change.

**Live production setup** (hosts, users, units, install/update/rollback of the real deployment) is kept
out of the public repo, in `~/.config/pap-analytics/SETUP.md` on `vpsagentfarma`. Read it before deploying;
it holds no secrets in the repo and must not be copied here.

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
| `[sources.sync] remote` | e.g. `pap-sync@nas:/` (rrsync) or `user@nas:/srv/aircannect/` | |
| `[sources.sync] ssh_key_file`, `known_hosts_file` | key (chmod 600) and pinned host key | `/run/secrets/pap_sync_key`, `/etc/pap-analytics/known_hosts` |
| `[sources.sync] interval_s` | used by `ingest --watch` (default 900, min 10) | |
| `[http] host, port, token_file` | HTTP transport; token required | `/run/secrets/pap_http_token` |

Env overrides: `PAP_CONFIG`, `PAP_STORE_DIR`, `PAP_DAY_BOUNDARY_HOUR`, `PAP_HTTP_HOST`, `PAP_HTTP_PORT`,
`PAP_HTTP_TOKEN` (token value), `PAP_HTTP_TOKEN_FILE`. `pap config show` prints the effective config; for key, known_hosts and token
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

## Source host access (sync over SSH)

Recommended setup when the data lives on another host (e.g. an AirCANnect share on `nas`):

| Host | Role |
|---|---|
| source host | holds the SD-card copy, e.g. `/srv/aircannect`; source only, nothing is deployed there |
| `pap` host | runs `pap` (sync + ingest, MCP); clients connect here |

- Dedicated user (e.g. `pap-sync`) on the source host with read access to the data directory only; no sudo.
- Dedicated ED25519 key generated on the `pap` host; the private key never leaves it (`chmod 600`).
- `authorized_keys` line on the source host restricts the key to a read-only rsync of that directory:
  `command="/usr/bin/rrsync -ro /srv/aircannect",restrict ssh-ed25519 AAAA… pap-sync@pap-host`
  (Debian's `rsync` package ships `/usr/bin/rrsync`).
- Reach the source host over a private network (VPN/tailnet). Pin its host key:
  `ssh-keyscan -t ed25519 nas > known_hosts` and verify the fingerprint out of band.
- With rrsync the root is the restricted directory, so `remote = "pap-sync@nas:/"`.
- Check: pulling works; uploads and arbitrary commands are refused.

Concrete hosts, addresses and the live setup are kept outside this public repository.

## Install, update, rollback

Merging does not deploy; release policy (who merges, what needs David) is in `AGENTS.md` → Delivery.

Native:
1. Install a **released tag**, never the development checkout:
   `uv tool install --force "git+https://github.com/drew2323/pap-analytics-mcp@vX.Y.Z"`.
2. Config and secrets (`chmod 600`), then `pap config show`.
3. `pap ingest` (first full sync + import); install the units from `deploy/systemd/` into
   `~/.config/systemd/user/`, `systemctl --user enable --now pap-ingest.timer`
   (user lingering must be on to survive logout/reboot: `loginctl enable-linger $USER`).
4. Update = repeat 1; rollback = `uv tool install --force …@vPREV`. A `SCHEMA_VERSION` change makes
   the store refuse to open: run `pap ingest --rebuild` (the store is disposable).
   Long-running stdio servers keep the old code in memory: restart the clients' MCP processes
   (e.g. a tunnel service, or end the `pap serve --stdio` children – agents respawn them on the next tool call).
   With a schema change: `systemctl --user stop pap-ingest.timer` → install → `pap ingest --rebuild`
   → start the timer → restart the servers.

Docker: `docker build -t pap-analytics-mcp:vX.Y.Z .`, set `PAP_VERSION`, `docker compose up -d`.
Rollback = previous image tag.

## Store rebuild, logs, troubleshooting

- Rebuild: `pap ingest --rebuild` (wipes only `store_dir` catalog and signals).
- Logs: native `journalctl --user -u pap-ingest.service`; Docker `docker compose logs ingest mcp`.
- **Stale data** ("data are N days old"): `list_devices` → `sources[]`. `last_sync_ok=false` → read
  `last_sync_error`; `last_sync_ok=true` but old `newest_therapy_day` → the source itself has no new night.
- **Sync auth failure** (`Permission denied`, `Host key verification failed`): check key permissions
  (`pap config show`), the `authorized_keys` line on the source host and `known_hosts`. Ingest keeps serving the last copy.
- **Lock conflicts**: tool error "data is being updated" is transient; persistent → a stuck `pap ingest`
  (`ps`, `store.lock` in `store_dir`).
- **Schema mismatch**: `pap ingest --rebuild`.

## Client integration

Use a separate HTTP token per client so either can be revoked. One `pap serve` process has one token, so run one
server instance (own port, own `PAP_HTTP_TOKEN_FILE`) per HTTP client. Stdio clients need no token.

| Client | Native | Docker |
|---|---|---|
| Hermes (`mcp_servers`) | `command: pap`, `args: [serve, --stdio, --read-only]`, `env: {PAP_CONFIG: …}` | `url: http://127.0.0.1:8765/mcp` + `headers: {Authorization: Bearer <token from token file>}` |
| ChatGPT via `tunnel-client` (own profile, own user unit, own runtime key) | `mcp.commands: [{channel: main, command: <launcher>}]` with `deploy/launchers/pap-mcp-stdio` | `--mcp.server-url url=http://127.0.0.1:8765/mcp,channel=main` + `--mcp.extra-headers 'Authorization: Bearer …'` (**unverified – test first**; fallback: the stdio launcher with `PAP_RUNTIME=docker`) |

Hermes example:

```yaml
mcp_servers:
  pap:                       # stdio
    command: pap
    args: ["serve", "--stdio", "--read-only"]
    env: {PAP_CONFIG: /home/me/.config/pap-analytics/config.toml}
  pap_http:                  # or HTTP
    url: http://127.0.0.1:8765/mcp
    headers: {Authorization: "Bearer ${PAP_HTTP_TOKEN}"}
```

Update: `uv tool install --force "git+https://github.com/drew2323/pap-analytics-mcp@vX.Y.Z" --with matplotlib`,
then restart the stdio servers (see above); rollback with the previous tag.
