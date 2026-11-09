#!/usr/bin/env bash
# Smoke test of a runtime against the committed fixture (no sync, no secrets from the host).
#   deploy/smoke.sh native   - `pap` from the checkout (uv run), stdio + HTTP
#   deploy/smoke.sh docker   - builds the image, runs compose (ingest + mcp), HTTP + stdio fallback
# Expect: night_summary(2026-11-08) device AHI 1.0, 401 without token, non-root, no secrets in layers.
set -euo pipefail
MODE="${1:?usage: smoke.sh native|docker}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE="$ROOT/tests/data/resmed-air11-autoset"
WORK="$(mktemp -d)"
PORT="${PAP_SMOKE_PORT:-18765}"
TOKEN="smoke-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
PIDS=""
cleanup() {
  [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
  if [ "$MODE" = docker ]; then (cd "$WORK" && docker compose -p papsmoke down -v >/dev/null 2>&1 || true); fi
  rm -rf "$WORK"
}
trap cleanup EXIT
ok() { echo "ok   $*"; }
fail() { echo "FAIL $*" >&2; exit 1; }

# MCP client check: $1 = url (http) or "stdio:<cmd...>"; prints device AHI of 2026-11-08
mcp_ahi() {
  (cd "$ROOT" && uv run python "$ROOT/deploy/smoke_client.py" "$@")
}

printf '%s' "$TOKEN" > "$WORK/token"; chmod 600 "$WORK/token"

if [ "$MODE" = native ]; then
  cat > "$WORK/config.toml" <<TOML
store_dir = "$WORK/store"
[[sources]]
name = "fixture"
path = "$FIXTURE"
[http]
host = "127.0.0.1"
port = $PORT
token_file = "$WORK/token"
TOML
  export PAP_CONFIG="$WORK/config.toml"
  PAP="$ROOT/.venv/bin/pap"
  $PAP config show | grep -q "$TOKEN" && fail "config show leaked the token"
  ok "config show redacts the token"
  $PAP ingest --no-sync >/dev/null || fail "ingest"
  ok "ingest"
  $PAP serve --http --read-only >"$WORK/serve.log" 2>&1 & PIDS="$!"
  for _ in $(seq 60); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || fail "healthz"
  ok "healthz"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/mcp")
  [ "$code" = 401 ] || fail "expected 401 without token, got $code"
  ok "401 without token"
  [ "$(mcp_ahi "http://127.0.0.1:$PORT/mcp" "$TOKEN")" = "1.0" ] || fail "HTTP night_summary AHI"
  ok "HTTP night_summary(2026-11-08) device AHI 1.0"
  [ "$(PAP_BIN="$ROOT/.venv/bin/pap" mcp_ahi stdio "$ROOT/deploy/launchers/pap-mcp-stdio")" = "1.0" ] || fail "stdio night_summary AHI"
  ok "stdio launcher night_summary AHI 1.0"
  echo "native smoke passed"
  exit 0
fi

# ---- docker ----
IMAGE="pap-analytics-mcp:smoke"
docker build -q -t "$IMAGE" "$ROOT" >/dev/null || fail "docker build"
ok "image built"
cp -r "$FIXTURE" "$WORK/source"; chmod -R a+rX "$WORK/source"
mkdir -p "$WORK/secrets"; cp "$WORK/token" "$WORK/secrets/pap_http_token"; chmod 644 "$WORK/secrets/pap_http_token"
echo "unused" > "$WORK/secrets/pap_sync_key"; chmod 644 "$WORK/secrets/pap_sync_key"; : > "$WORK/secrets/known_hosts"
cat > "$WORK/config.toml" <<TOML
store_dir = "/data/store"
[[sources]]
name = "fixture"
path = "/data/source/fixture"
[http]
host = "0.0.0.0"
port = 8765
token_file = "/run/secrets/pap_http_token"
TOML
# the compose example with the fixture bind-mounted as source and a test image/port
sed -e "s#image: pap-analytics-mcp:\${PAP_VERSION:-latest}#image: $IMAGE#" -e '/^    build: \./d' \
    -e "s#127.0.0.1:8765:8765#127.0.0.1:$PORT:8765#" \
    -e "s#      - source:/data/source:ro#      - ./source:/data/source/fixture:ro#" \
    -e "s#      - source:/data/source\$#      - ./source:/data/source/fixture:ro#" \
    "$ROOT/compose.example.yaml" > "$WORK/compose.yaml"
cd "$WORK"
# the ingest service in production runs `ingest --watch`; the smoke runs it once without sync
docker run --rm -v "$WORK/config.toml:/etc/pap-analytics/config.toml:ro" -v "$WORK/source:/data/source/fixture:ro" \
  -v papsmoke_store:/data/store "$IMAGE" ingest --no-sync >/dev/null || fail "docker ingest"
docker volume inspect papsmoke_store >/dev/null && ok "ingest in container"
docker compose -p papsmoke up -d mcp >/dev/null 2>&1 || fail "compose up"
for _ in $(seq 60); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || { docker compose -p papsmoke logs mcp | tail -20; fail "healthz"; }
ok "healthz"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/mcp")
[ "$code" = 401 ] || fail "expected 401 without token, got $code"
ok "401 without token"
[ "$(mcp_ahi "http://127.0.0.1:$PORT/mcp" "$TOKEN")" = "1.0" ] || fail "HTTP night_summary AHI"
ok "HTTP night_summary(2026-11-08) device AHI 1.0"
[ "$(mcp_ahi stdio docker compose -p papsmoke exec -T mcp pap serve --stdio --read-only)" = "1.0" ] \
  || fail "stdio fallback night_summary AHI"
ok "stdio fallback via docker compose exec -T"
uid=$(docker compose -p papsmoke exec -T mcp id -u)
[ "$uid" != 0 ] || fail "container runs as root"
ok "non-root (uid $uid)"
if docker history --no-trunc "$IMAGE" | grep -qiE "$TOKEN|BEGIN [A-Z ]*PRIVATE KEY|ed25519"; then fail "secret-like text in image history"; fi
docker run --rm --entrypoint sh "$IMAGE" -c 'find / -xdev \( -name "*_ed25519" -o -name "*.token" -o -name ".env" \) 2>/dev/null | grep -q . && exit 1 || exit 0' \
  || fail "secret-like file in image"
ok "no secrets in image layers"
echo "docker smoke passed"
