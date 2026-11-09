"""Bearer-token authentication for the HTTP transport (ASGI middleware, fail-closed).

``/healthz`` is public and returns no data. Every other path needs ``Authorization: Bearer <token>``;
tokens are compared in constant time. Boundaries: no MCP or store knowledge here.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from pap_analytics.config import HttpConfig

HEALTH_PATH = "/healthz"


class AuthConfigError(RuntimeError):
    """HTTP transport cannot start safely (no token, non-loopback without auth, ...)."""


def load_token(http: HttpConfig, env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    if env.get("PAP_HTTP_TOKEN"):
        return env["PAP_HTTP_TOKEN"].strip()
    if http.token_file is not None:
        try:
            token = Path(http.token_file).expanduser().read_text().strip()
        except OSError as exc:
            raise AuthConfigError(f"cannot read token file {http.token_file}: {exc}") from exc
        if token:
            return token
    return None


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def resolve_auth(http: HttpConfig, insecure_no_auth: bool, env: dict[str, str] | None = None) -> str | None:
    """Token to enforce, or ``None`` when auth is explicitly disabled on loopback."""
    token = load_token(http, env)
    if token:
        return token
    if insecure_no_auth:
        if not is_loopback(http.host):
            raise AuthConfigError("--insecure-no-auth is only allowed when the host is loopback")
        return None
    raise AuthConfigError(
        "HTTP transport needs a bearer token: set PAP_HTTP_TOKEN or http.token_file "
        "(or --insecure-no-auth on a loopback host)"
    )


class BearerAuthMiddleware:
    def __init__(self, app: Any, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == HEALTH_PATH:
            await _respond(send, 200, b'{"status":"ok"}')
            return
        if self.token is not None:
            header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            scheme, _, given = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), self.token.encode()):
                await _respond(send, 401, b'{"error":"unauthorized"}', [(b"www-authenticate", b"Bearer")])
                return
        await self.app(scope, receive, send)


async def _respond(
    send: Any, status: int, body: bytes, extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": body})
