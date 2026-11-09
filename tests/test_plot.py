import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

pytest.importorskip("matplotlib")

from pap_analytics.analytics import AnalyticsError, Context  # noqa: E402
from pap_analytics.analytics.plot import MAX_BYTES, plot_segment  # noqa: E402
from pap_analytics.analytics.timeparse import parse_time  # noqa: E402
from pap_analytics.config import Config  # noqa: E402
from pap_analytics.mcp_server import build_server  # noqa: E402
from pap_analytics.store import Store  # noqa: E402

DEV = "resmed-23262018278"
CTX = Context()
PNG = b"\x89PNG\r\n\x1a\n"


def window(a: str, b: str) -> tuple[datetime, datetime]:
    return parse_time(a, date(2026, 11, 8), CTX), parse_time(b, date(2026, 11, 8), CTX)


def test_plot_segment_png(store: Store) -> None:
    png = plot_segment(store, DEV, *window("05:00", "05:40"), ctx=CTX)
    assert png.startswith(PNG) and 5_000 < len(png) < MAX_BYTES


def test_plot_whole_night_and_channels(store: Store) -> None:
    png = plot_segment(store, DEV, *window("23:45", "05:50"), ["leak", "spo2"], CTX)
    assert png.startswith(PNG) and len(png) < MAX_BYTES
    assert plot_segment(store, DEV, *window("05:00", "05:10"), ["spo2"], CTX).startswith(PNG)


def test_plot_errors(store: Store) -> None:
    with pytest.raises(AnalyticsError, match="unknown channels"):
        plot_segment(store, DEV, *window("05:00", "05:10"), ["bogus"], CTX)
    with pytest.raises(AnalyticsError, match="up to 30 min"):
        plot_segment(store, DEV, *window("02:00", "04:00"), ["flow"], CTX)
    with pytest.raises(AnalyticsError, match="no channel data"):
        plot_segment(store, DEV, *window("21:00", "21:30"), ["leak"], CTX)
    t = datetime(2026, 11, 9, 4, tzinfo=UTC)
    with pytest.raises(AnalyticsError, match="after start"):
        plot_segment(store, DEV, t, t - timedelta(minutes=1), None, CTX)


async def test_mcp_plot_tool_returns_image(ingested_store: Path) -> None:
    server = build_server(Config(store_dir=ingested_store))
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        res = await s.call_tool("plot_segment", {"date": "2026-11-08", "start": "05:00", "end": "05:40"})
        assert not res.isError and res.content[0].type == "image" and res.content[0].mimeType == "image/png"
        res = await s.call_tool("find_anomalies", {"date": "2026-11-08"})
        assert not res.isError and res.structuredContent["anomalies"]


async def test_plot_tool_absent_without_matplotlib(
    ingested_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    server = build_server(Config(store_dir=ingested_store))
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert "find_anomalies" in names and "plot_segment" not in names
