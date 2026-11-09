"""`pap serve`: run the MCP server over stdio or authenticated streamable HTTP."""

from __future__ import annotations

import argparse
import dataclasses
from typing import Any

from pap_analytics import cli
from pap_analytics.config import Config
from pap_analytics.mcp_auth import AuthConfigError, BearerAuthMiddleware, resolve_auth


def build_http_app(config: Config, *, read_only: bool, insecure_no_auth: bool) -> Any:
    """ASGI app: bearer auth in front of the MCP endpoint (`/mcp`) plus public `/healthz`."""
    from pap_analytics.mcp_server import build_server

    token = resolve_auth(config.http, insecure_no_auth)
    server = build_server(config, read_only=read_only, host=config.http.host, port=config.http.port)
    return BearerAuthMiddleware(server.streamable_http_app(), token)


def _serve(args: argparse.Namespace, config: Config) -> int:
    from pap_analytics.mcp_server import build_server

    http = dataclasses.replace(
        config.http,
        host=args.host or config.http.host,
        port=args.port or config.http.port,
    )
    config = dataclasses.replace(config, http=http)
    if not args.http:
        build_server(config, read_only=args.read_only).run("stdio")
        return 0
    import uvicorn

    try:
        app = build_http_app(config, read_only=args.read_only, insecure_no_auth=args.insecure_no_auth)
    except AuthConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    uvicorn.run(app, host=http.host, port=http.port, log_level="info")
    return 0


def register(subparsers: Any) -> None:
    p = subparsers.add_parser("serve", help="run the MCP server")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="stdio transport (default)")
    mode.add_argument("--http", action="store_true", help="streamable HTTP with bearer token")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--read-only", action="store_true", help="do not register the admin `ingest` tool")
    p.add_argument("--insecure-no-auth", action="store_true", help="HTTP without token (loopback only)")
    p.set_defaults(func=_serve)


cli.add_command(register)
