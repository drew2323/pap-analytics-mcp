"""Readers while a writer ingests: only retry or a clear ToolError, never a crash."""

import subprocess
import sys
import threading
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from pap_analytics.config import Config, SourceConfig
from pap_analytics.ingest import ingest
from pap_analytics.mcp_server import build_server

RUN = "import sys; from pap_analytics.cli import main; sys.exit(main())"
OK_ERRORS = ("data is being updated", "No data yet", "store is empty")  # last: during --rebuild


async def test_ingest_during_repeated_night_summary(
    tmp_path: Path, resmed_air11_str_history: Path, resmed_air11: Path
) -> None:
    store_dir = tmp_path / "st"
    cfg = Config(sources=[SourceConfig(path=resmed_air11_str_history)], store_dir=store_dir)
    ingest(cfg)
    server = build_server(Config(store_dir=store_dir))
    failures: list[str] = []
    done = threading.Event()

    conf = tmp_path / "c.toml"
    conf.write_text(f'store_dir = "{store_dir}"\n[[sources]]\npath = "{resmed_air11}"\n')

    def writer() -> None:  # a separate process, like `pap ingest` next to `pap serve`
        try:
            for flag in ([], ["--prune"], ["--rebuild"]):
                cmd = [sys.executable, "-c", RUN, "--config", str(conf), "ingest", *flag]
                p = subprocess.run(cmd, capture_output=True, text=True)
                if p.returncode != 0:
                    failures.append(f"writer {flag}: {p.stdout[-300:]}{p.stderr[-300:]}")
        finally:
            done.set()

    t = threading.Thread(target=writer)
    ok = errors = 0
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        t.start()
        while not done.is_set() or ok + errors < 5:
            res = await s.call_tool("night_summary", {"date": "2026-10-16"})
            if res.isError:
                msg = res.content[0].text
                if not any(m in msg for m in OK_ERRORS) and "nearest available" not in msg:
                    failures.append(msg)
                errors += 1
            else:
                ok += 1
        t.join()
    assert not failures, failures
    assert ok > 0
