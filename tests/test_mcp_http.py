import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from pap_analytics.cli import main
from pap_analytics.cli_serve import build_http_app
from pap_analytics.config import Config, HttpConfig
from pap_analytics.mcp_auth import AuthConfigError, resolve_auth

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}  # fmt: skip
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def server(ingested_store: Path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = Config(store_dir=ingested_store, http=HttpConfig("127.0.0.1", port, None))
    import os

    os.environ["PAP_HTTP_TOKEN"] = "s3cret"
    try:
        app = build_http_app(config, read_only=True, insecure_no_auth=False)
    finally:
        del os.environ["PAP_HTTP_TOKEN"]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def test_auth_and_health(server: str) -> None:
    assert httpx.get(server + "/healthz").status_code == 200
    assert httpx.get(server + "/healthz").text == '{"status":"ok"}'
    r = httpx.post(server + "/mcp", json=INIT, headers=HEADERS)
    assert r.status_code == 401
    r = httpx.post(server + "/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = httpx.post(server + "/mcp", json=INIT, headers={**HEADERS, "authorization": "Basic s3cret"})
    assert r.status_code == 401


async def test_tool_list_over_http(server: str) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with (
        streamablehttp_client(server + "/mcp", headers={"authorization": "Bearer s3cret"}) as (
            r,
            w,
            _,
        ),
        ClientSession(r, w) as s,
    ):
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert "night_summary" in names and "ingest" not in names
        res = await s.call_tool("night_summary", {"date": "2026-11-08"})
        assert not res.isError


def test_fail_closed_configuration(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    with pytest.raises(AuthConfigError, match="bearer token"):
        resolve_auth(HttpConfig(), False, env)
    with pytest.raises(AuthConfigError, match="loopback"):
        resolve_auth(HttpConfig(host="0.0.0.0"), True, env)
    assert resolve_auth(HttpConfig(), True, env) is None
    tf = tmp_path / "tok"
    tf.write_text("abc\n")
    assert resolve_auth(HttpConfig(token_file=tf), False, env) == "abc"
    assert resolve_auth(HttpConfig(token_file=tf), False, {"PAP_HTTP_TOKEN": "env"}) == "env"
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(AuthConfigError):
        resolve_auth(HttpConfig(token_file=empty), False, env)
    with pytest.raises(AuthConfigError, match="cannot read"):
        resolve_auth(HttpConfig(token_file=tmp_path / "missing"), False, env)


def test_cli_serve_refuses_without_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("PAP_HTTP_TOKEN", raising=False)
    conf = tmp_path / "c.toml"
    conf.write_text(f'store_dir = "{tmp_path}"\n')
    assert main(["--config", str(conf), "serve", "--http"]) == 2
    assert "bearer token" in capsys.readouterr().out
