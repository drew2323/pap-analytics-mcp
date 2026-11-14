import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from pap_analytics.config import Config
from pap_analytics.mcp_server import build_server

TOOLS = {
    "list_devices", "list_nights", "night_summary", "get_events", "segment_stats", "get_signal",
    "get_signals", "oximetry_summary", "disturbance_summary", "disturbance_trend", "compare_periods",
    "get_settings", "find_anomalies", "plot_segment",
}  # fmt: skip


def cfg(store_dir: Path) -> Config:
    return Config(store_dir=store_dir)


async def call(session, name: str, **args):
    res = await session.call_tool(name, args)
    if res.isError:
        return res, None
    return res, res.structuredContent if res.structuredContent is not None else json.loads(
        res.content[0].text
    )


def with_session(fn):
    """Open an in-process client session inside the test task (anyio scopes must not cross tasks)."""

    async def wrapper(ingested_store: Path) -> None:
        server = build_server(cfg(ingested_store))
        async with create_connected_server_and_client_session(server._mcp_server) as s:
            await fn(s)

    wrapper.__name__ = fn.__name__
    return wrapper


async def test_tool_list_exact(ingested_store: Path) -> None:
    for read_only, expected in ((False, TOOLS | {"ingest"}), (True, TOOLS)):
        server = build_server(cfg(ingested_store), read_only=read_only)
        async with create_connected_server_and_client_session(server._mcp_server) as s:
            tools = (await s.list_tools()).tools
            assert {t.name for t in tools} == expected
            assert all(t.description for t in tools)


@with_session
async def test_every_tool_callable_and_matches_schema(session) -> None:
    import jsonschema  # noqa: F401  # provided by the mcp dependency tree

    calls = {
        "list_devices": {},
        "list_nights": {},
        "night_summary": {"date": "last"},
        "get_events": {"date": "2026-11-08", "types": ["hypopnea"]},
        "segment_stats": {"date": "2026-11-08", "start": "02:00", "end": "02:30"},
        "get_signal": {"date": "2026-11-08", "start": "02:00", "end": "03:00", "channel": "leak"},
        "oximetry_summary": {"date": "2026-11-08"},
        "disturbance_summary": {"date": "2026-11-08"},
        "disturbance_trend": {"date_from": "2026-11-03", "date_to": "2026-11-08"},
        "compare_periods": {
            "a_from": "2026-10-16", "a_to": "2026-10-19",
            "b_from": "2026-10-20", "b_to": "2026-10-25",
        },
        "get_settings": {},
        "find_anomalies": {"date": "2026-11-08"},
    }  # fmt: skip
    tools = {t.name: t for t in (await session.list_tools()).tools}
    for name, args in calls.items():
        res, data = await call(session, name, **args)
        assert not res.isError, (name, res.content)
        assert isinstance(data, dict)
        schema = tools[name].outputSchema
        if schema:
            jsonschema.validate(data, schema)


@with_session
async def test_night_summary_content(session) -> None:
    _, d = await call(session, "night_summary", date="2026-11-08")
    assert d["device_metrics"]["ahi"]["value"] == 1.0 and d["device_metrics"]["ahi"]["provenance"] == "device"
    assert len(d["sessions"]) == 2
    assert d["oximetry"]["metrics"]["spo2_min"]["value"] == 89
    assert d["sessions"][1]["start"].endswith("+01:00")


@with_session
async def test_disturbance_tools_separate_measured_and_inferred(session) -> None:
    _, d = await call(session, "disturbance_summary", date="2026-11-08", start="23:00", end="06:00")
    assert d["measured"]["prr_median"]["provenance"] == "derived"
    assert d["measured"]["hypoxic_burden"]["unit"] == "%min/h"
    assert "autonomic_arousal_index" in d["inferred"] and "prr_median" not in d["inferred"]
    assert all(f["start"].endswith("+01:00") for f in d["findings"])
    _, t = await call(session, "disturbance_trend", date_from="2026-11-03", date_to="2026-11-08")
    assert [n["date"] for n in t["nights"]][-1] == "2026-11-08"
    assert t["metrics"]["rdi_inferred"]["basis"] == "inferred"
    for key in ("calm_breathing_pct", "longest_calm_min", "restless_periods_per_h", "breathing_coverage_pct"):
        assert key in d["measured"] and t["metrics"][key]["basis"] == "measured"
    assert any(f["kind"] == "restless_breathing" for f in d["findings"])
    for key in ("ned_mean_pct", "glasgow_index", "glasgow_variable_amp_pct"):
        assert key in d["measured"] and t["metrics"][key]["basis"] == "measured"
    assert set(d["flow_shape_by_ps"]) == {"ps_0", "ps_gt_0"}
    assert t["metrics"]["ned_rera_index"]["basis"] == "inferred"
    res, _ = await call(session, "disturbance_trend", date_from="2026-11-08", date_to="2026-11-03")
    assert res.isError

@with_session
async def test_compare_periods_with_disturbances(session) -> None:
    args = {"a_from": "2026-11-07", "a_to": "2026-11-07", "b_from": "2026-11-08", "b_to": "2026-11-08"}
    _, plain = await call(session, "compare_periods", **args)
    assert "glasgow_index" not in plain["metrics"]
    _, d = await call(session, "compare_periods", include_disturbances=True, **args)
    g = d["metrics"]["glasgow_index"]
    assert g["a_n"] == g["b_n"] == 1 and g["delta"] == pytest.approx(g["b_mean"] - g["a_mean"], abs=1e-3)
    assert d["metrics"]["ahi"]["b_n"] == 1

@with_session
async def test_segment_stats_local_times(session) -> None:
    _, d = await call(session, "segment_stats", date="2026-11-08", start="02:00", end="02:30")
    assert d["start"] == "2026-11-09T02:00:00+01:00" and d["channels"]["leak"]["coverage_pct"] == 100.0


@with_session
async def test_get_signal_small_and_capped(session) -> None:
    res, d = await call(session, "get_signal", date="2026-11-08", start="23:45", end="05:50", channel="leak")
    assert len(res.content[0].text) < 40_000 and len(d["t"]) == 300
    _, big = await call(session, "get_signal", date="2026-11-08", start="23:45", end="05:50", channel="leak",
                        max_points=5000)  # fmt: skip
    assert len(big["t"]) == 2000


@with_session
async def test_errors_are_actionable(session) -> None:
    res, _ = await call(session, "night_summary", date="2026-12-24")
    assert (
        res.isError
        and "nearest available days" in res.content[0].text
        and "2026-11-08" in res.content[0].text
    )
    res, _ = await call(session, "get_signal", date="2026-11-08", start="02:00", end="04:00", channel="flow")
    assert res.isError and "shorter window" in res.content[0].text
    res, _ = await call(session, "get_signal", date="2026-11-08", start="02:00", end="03:00", channel="bogus")
    assert res.isError and "channels available" in res.content[0].text and "leak" in res.content[0].text
    res, _ = await call(session, "get_events", date="2026-11-08", types=["nope"])
    assert res.isError and "valid" in res.content[0].text
    res, _ = await call(session, "night_summary", date="x", device_id="y")
    assert res.isError and "unknown device" in res.content[0].text
    res, _ = await call(session, "segment_stats", date="2026-11-08", start="99:00", end="02:30")
    assert res.isError


@with_session
async def test_oximetry_unavailable_day(session) -> None:
    _, d = await call(session, "oximetry_summary", date="2026-11-04")
    assert d["available"] is False


@with_session
async def test_settings_and_resources_and_prompt(session) -> None:
    _, d = await call(session, "get_settings", date="2026-11-08")
    assert d["in_effect"]["mode"] == "asv"
    assert d["session_mode"] == "asv" and "applies" not in d["in_effect"] and d["notes"] == []
    _, d = await call(session, "get_settings", date="2026-11-03")
    assert d["session_mode"] == "cpap" and d["in_effect"]["applies"] is False and d["notes"]
    _, d = await call(session, "night_summary", date="2026-11-03")
    assert d["mode"] == "cpap"
    glossary = (await session.read_resource("pap://glossary")).contents[0].text
    assert "Therapy mode" in glossary
    assert "Therapy day" in glossary and "not a diagnosis" in glossary and "tidal_volume" in glossary
    assert "include_disturbances=true" in glossary and "not sleep time" in glossary
    devices = json.loads((await session.read_resource("pap://devices")).contents[0].text)
    assert devices[0]["model"] == "AirSense11AutoSet"
    p = await session.get_prompt("nightly_review", {"date": "2026-11-08"})
    text = p.messages[0].content.text
    assert "night_summary" in text and "provenance" in text and "diagnosis" in text
    assert "disturbance_trend" in text and "include_disturbances=true" in text


async def test_no_store_gives_actionable_error(tmp_path: Path) -> None:
    server = build_server(cfg(tmp_path / "none"))
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        res, _ = await call(s, "list_devices")
        assert res.isError and "pap ingest" in res.content[0].text


async def test_ingest_tool_and_new_data_visible_without_restart(
    tmp_path: Path, resmed_air11_str_history: Path
) -> None:
    config = Config(store_dir=tmp_path / "st")
    server = build_server(config)
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        res, _ = await call(s, "list_devices")
        assert res.isError
        res, d = await call(s, "ingest", source=str(resmed_air11_str_history))
        assert not res.isError and d["reports"][0]["therapy_days"] == 10
        res, d = await call(s, "list_devices")
        assert d["devices"][0]["last_day"] == "2026-10-25"


async def test_list_devices_reports_sync_and_ingest_status(ingested_store: Path, tmp_path: Path) -> None:
    import json as _json

    from pap_analytics.config import SourceConfig, SyncConfig
    from pap_analytics.sync import STATE_FILE

    sy = SyncConfig("r:/x/", tmp_path / "k", tmp_path / "h")
    src = SourceConfig(path=tmp_path / "s", name="aircannect", sync=sy)
    plain = SourceConfig(path=tmp_path / "p", name="local")
    store_dir = tmp_path / "st"
    import shutil

    shutil.copytree(ingested_store, store_dir)
    (store_dir / STATE_FILE).write_text(
        _json.dumps({"aircannect": {"last_sync_at": "2026-11-09T06:00:00+00:00", "last_sync_ok": False,
                                    "error": "boom"}})
    )  # fmt: skip
    server = build_server(Config(sources=[src, plain], store_dir=store_dir))
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        _, d = await call(s, "list_devices")
    a, b = d["sources"]
    assert a["name"] == "aircannect" and a["has_sync"] and a["last_sync_ok"] is False
    assert a["last_sync_at"].startswith("2026-11-09T06:00") and a["last_sync_error"] == "boom"
    assert a["last_ingest_at"] and a["newest_therapy_day"] == "2026-11-08"
    assert b["has_sync"] is False and b["last_sync_at"] is None and b["last_sync_ok"] is None


@with_session
async def test_get_signals_tool(session) -> None:
    res, d = await call(session, "get_signals", date="2026-11-08", start="00:10", end="00:11",
                        channels=["flow", "pressure_hr", "mask_pressure", "pressure", "spo2"])  # fmt: skip
    assert not res.isError and d["step_s"] == 0.04 and d["rows"] == 1500
    res, d = await call(session, "get_signals", date="2026-11-08", start="00:10", end="00:10:30",
                        channels=["flow", "pressure_hr", "mask_pressure", "pressure", "spo2"])  # fmt: skip
    assert (
        not res.isError
        and d["step_s"] == 0.04
        and set(d["channels"]) <= {"flow", "pressure_hr", "mask_pressure", "pressure", "spo2"}
    )
    assert len(res.content[0].text) < 60_000
    res, _ = await call(
        session, "get_signals", date="2026-11-08", start="00:10", end="00:11", channels=["nope"]
    )
    assert res.isError and "channels available" in res.content[0].text
    res, _ = await call(
        session, "get_signals", date="2026-11-08", start="00:10", end="00:30", channels=["flow"]
    )
    assert res.isError and "limited to 2000 rows" in res.content[0].text
