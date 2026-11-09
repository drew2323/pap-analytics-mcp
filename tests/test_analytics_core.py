import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from pap_analytics.analytics import (
    AnalyticsError,
    Context,
    compare_periods,
    downsample,
    event_clusters,
    get_events,
    get_signal,
    list_nights,
    night_summary,
    parse_day,
    parse_time,
    segment_stats,
    settings_history,
    to_dict,
)
from pap_analytics.analytics.results import NightRow
from pap_analytics.analytics.segments import STAT_CHANNELS
from pap_analytics.analytics.timeparse import day_window
from pap_analytics.model import ChannelData, Event, EventType, Metric, Provenance
from pap_analytics.store import Store

CTX = Context()
HOUR = timedelta(hours=1)
DEV = "resmed-23262018278"
STR_TABLE = {  # tests/data/README.md: day -> (duration min, AHI)
    date(2026, 10, 16): (349, 1.2), date(2026, 10, 17): (434, 0.4), date(2026, 10, 19): (419, 2.0),
    date(2026, 10, 20): (362, 4.3), date(2026, 10, 21): (430, 2.9), date(2026, 10, 22): (435, 4.6),
    date(2026, 10, 23): (390, 1.5), date(2026, 10, 24): (404, 0.4), date(2026, 10, 25): (355, 0.5),
}  # fmt: skip


def test_night_summary_2026_11_08(store: Store, resmed_air11) -> None:
    from pap_analytics.edf.reader import EdfFile

    n = night_summary(store, DEV, date(2026, 11, 8), CTX)
    assert len(n.sessions) == 2
    assert n.device_metrics["ahi"].value == 1.0 and n.device_metrics["ahi"].provenance is Provenance.DEVICE
    # independent count from raw EVE annotations
    raw = 0
    for eve in (resmed_air11 / "DATALOG" / "20261108").glob("*_EVE.edf"):
        f = EdfFile.open(eve)
        raw += sum(1 for a in f.annotations() if a.text in ("Hypopnea", "Apnea", "Central Apnea"))
    assert raw == 7
    usage_h = n.derived_metrics["usage_min"].value / 60
    assert n.derived_metrics["ahi"].value == pytest.approx(raw / usage_h)
    assert n.derived_metrics["ahi"].provenance is Provenance.DERIVED
    assert n.events_by_type == {"apnea": 1, "hypopnea": 6}
    assert len(n.mask_off_gaps) == 1 and n.mask_off_gaps[0]["minutes"] == pytest.approx(188.4, abs=0.1)
    assert n.settings is not None and n.settings["mode"] == "asv"
    assert {"pressure", "leak", "resp_rate"} <= set(n.channels)
    assert n.channels["leak"].p5 is not None and n.derived_metrics["large_leak_min"].value == 0
    assert n.oximetry is not None and n.oximetry["metrics"]["spo2_min"].value == 89
    assert sum(r["events"] for r in n.events_per_hour) == 7


def test_night_summary_without_str_or_sessions(store: Store) -> None:
    n = night_summary(store, DEV, date(2026, 10, 27), CTX)
    assert any("no device summary" in x for x in n.notes) and n.device_metrics == {}
    n = night_summary(store, DEV, date(2020, 1, 1), CTX)
    assert n.sessions == [] and any("no detailed sessions" in x for x in n.notes)


def test_list_nights_matches_str_table(store: Store) -> None:
    rows = {r.date: r for r in list_nights(store, DEV, date(2026, 10, 16), date(2026, 10, 25), CTX)}
    for day, (dur, ahi) in STR_TABLE.items():
        r = rows[day]
        assert (
            r.ahi_device is not None
            and r.ahi_device.value == pytest.approx(ahi)
            and r.ahi_device.provenance is Provenance.DEVICE
        )
        assert r.usage_min.value == dur
        assert r.usage_min_derived is not None and abs(r.usage_min_derived.value - dur) <= 2
        assert r.ahi_derived.provenance is Provenance.DERIVED
    assert rows[date(2026, 10, 16)].has_oximetry and not rows[date(2026, 10, 22)].has_oximetry
    assert rows[date(2026, 10, 16)].leak_p95.value == pytest.approx(0.72)


def test_list_nights_without_device_summary_falls_back(store: Store) -> None:
    (r,) = list_nights(store, DEV, date(2026, 11, 3), date(2026, 11, 3), CTX)
    assert r.ahi_device is None and r.usage_min.provenance is Provenance.DERIVED and r.has_oximetry
    assert r.events["central_apnea"] == 1


def test_segment_stats_and_events_match_manual_filter(store: Store) -> None:
    a, b = parse_time("02:00", date(2026, 11, 8), CTX), parse_time("02:30", date(2026, 11, 8), CTX)
    assert a == datetime(2026, 11, 9, 1, 0, tzinfo=UTC)
    seg = segment_stats(store, DEV, a, b, CTX)
    for name in STAT_CHANNELS:
        assert seg.channels[name].coverage_pct == 100.0, name
    manual = [e for e in store.events(DEV, a - HOUR, b + HOUR) if a <= e.start < b]
    assert [e["start"] for e in seg.events] == [e.start for e in manual]
    with pytest.raises(AnalyticsError):
        segment_stats(store, DEV, a, b, CTX, channels=["nope"])
    gap = segment_stats(
        store, DEV, datetime(2026, 11, 8, 21, 0, tzinfo=UTC), datetime(2026, 11, 8, 21, 30, tzinfo=UTC), CTX
    )
    assert gap.channels == {} and gap.notes


def _ch(values, rate=0.5) -> ChannelData:
    return ChannelData(
        "leak", "L/s", rate, datetime(2026, 1, 1, tzinfo=UTC), np.asarray(values, dtype=np.float32)
    )


def test_downsample_8h_to_300_buckets() -> None:
    n = 8 * 3600 // 2
    rng = np.random.default_rng(0)
    v = rng.random(n).astype(np.float32)
    v[:48] = np.nan  # the first bucket (96 s = 48 samples) is NaN only
    r = downsample(_ch(v), 300)
    assert len(r.t) == len(r.min) == 300 and r.bucket_s == pytest.approx(96.0)
    assert r.min[0] is None and r.mean[0] is None and r.max[0] is None
    for lo, m, hi in zip(r.min[1:], r.mean[1:], r.max[1:], strict=True):
        assert lo <= m <= hi
    assert max(x for x in r.max if x is not None) == pytest.approx(float(np.nanmax(v)))


def test_downsample_short_series_keeps_native() -> None:
    r = downsample(_ch([1, 2, 3]), 300)
    assert r.mean == [1.0, 2.0, 3.0] and r.bucket_s == 2.0


def test_get_signal_limits(store: Store) -> None:
    a = parse_time("02:00", date(2026, 11, 8), CTX)
    with pytest.raises(AnalyticsError, match="shorter window"):
        get_signal(store, DEV, a, a + timedelta(hours=2), "flow")
    ok = get_signal(store, DEV, a, a + timedelta(minutes=10), "flow", max_points=100000)
    assert len(ok.t) == 2000 and ok.notes
    p = get_signal(store, DEV, a, a + timedelta(hours=2), "pressure", 300)
    assert len(p.t) == 300 and p.unit == "cmH2O"
    with pytest.raises(AnalyticsError, match="unknown channel"):
        get_signal(store, DEV, a, a + timedelta(minutes=1), "zzz")
    with pytest.raises(AnalyticsError, match="no leak data"):
        get_signal(store, DEV, a - timedelta(days=100), a - timedelta(days=100, hours=-1), "leak")


def _ev(minute: float, t=EventType.HYPOPNEA, dur=10.0) -> Event:
    return Event(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute), dur, t, "x", Provenance.DEVICE)


def test_event_clusters() -> None:
    evs = [
        _ev(0),
        _ev(3),
        _ev(8, EventType.APNEA),
        _ev(60),
        _ev(100),
        _ev(105),
        _ev(111),
        _ev(120, EventType.RECORDING_START),
    ]
    cl = event_clusters(evs, min_events=3, window_min=10)
    assert [c.count for c in cl] == [3, 3]
    assert cl[0].by_type == {"hypopnea": 2, "apnea": 1}
    assert event_clusters(evs, min_events=4) == []


def test_get_events_limit(store: Store) -> None:
    a, b = day_window(date(2026, 10, 20), CTX)
    full = get_events(store, DEV, a, b)
    assert full.total == full.returned == 30  # 26 respiratory + 4 recording starts
    lim = get_events(store, DEV, a, b, {EventType.HYPOPNEA}, limit=5)
    assert lim.returned == 5 and lim.total == 21 and lim.notes


def test_compare_periods_exact_deltas() -> None:
    def row(day: int, usage: float, ahi: float, leak: float | None) -> NightRow:
        m = lambda v, u, p=Provenance.DERIVED: Metric(v, u, p)  # noqa: E731
        dev = m(ahi, "1/h", Provenance.DEVICE)
        return NightRow(date(2026, 1, day), m(usage, "min"), 1, dev, m(0.0, "1/h"),
                        {}, m(leak, "L/s") if leak is not None else None, None, False)  # fmt: skip

    a = [row(1, 400, 2.0, 0.2), row(2, 200, 4.0, None)]
    b = [row(3, 450, 1.0, 0.4), row(4, 350, 2.0, 0.2)]
    c = compare_periods(a, b)
    assert c.metrics["usage_min"]["a_mean"] == 300 and c.metrics["usage_min"]["b_mean"] == 400
    assert c.metrics["usage_min"]["delta"] == 100 and c.metrics["usage_min"]["delta_pct"] == pytest.approx(
        100 / 3
    )
    assert c.metrics["ahi"]["delta"] == -1.5 and c.metrics["ahi"]["delta_pct"] == -50
    assert c.metrics["leak_p95"]["a_n"] == 1 and c.metrics["leak_p95"]["delta"] == pytest.approx(0.1)
    assert c.metrics["pressure_p95"]["delta"] is None
    assert compare_periods([], b).notes


def test_settings_history(store: Store) -> None:
    h = settings_history(store, DEV)
    assert h and h[-1]["mode"] == "asv" and "mode" in h[0]["changed"]


def test_timeparse() -> None:
    d = date(2026, 11, 8)
    assert parse_time("20:00", d, CTX).astimezone(CTX.tz).day == 8
    assert parse_time("02:10", d, CTX).astimezone(CTX.tz).replace(tzinfo=None) == datetime(2026, 11, 9, 2, 10)
    assert parse_time("2026-11-09T03:00:00", d, CTX) == datetime(2026, 11, 9, 2, 0, tzinfo=UTC)
    assert parse_time("2026-11-09T03:00:00+00:00", d, CTX) == datetime(2026, 11, 9, 3, 0, tzinfo=UTC)
    for bad in ("25:00", "abc"):
        with pytest.raises(AnalyticsError):
            parse_time(bad, d, CTX)
    assert parse_day("2026-11-08", CTX) == d
    now = datetime(2026, 11, 10, 8, 0, tzinfo=ZoneInfo("Europe/Prague"))
    assert parse_day("yesterday", CTX, now=now) == date(2026, 11, 9)
    with pytest.raises(AnalyticsError):
        parse_day("tomorrow", CTX)
    lo, hi = day_window(d, CTX)
    assert (hi - lo) == timedelta(hours=24) and lo == datetime(2026, 11, 8, 11, 0, tzinfo=UTC)


def test_parse_last(store: Store) -> None:
    assert parse_day("last", CTX, store, DEV) == date(2026, 11, 8)


def test_to_dict_is_json_with_local_isoformat(store: Store) -> None:
    n = night_summary(store, DEV, date(2026, 11, 8), CTX)
    d = to_dict(n, CTX.tz)
    text = json.dumps(d, allow_nan=False)
    assert "+01:00" in text and list(d)[:3] == ["date", "device_id", "sessions"]
    assert d["device_metrics"]["ahi"] == {"value": 1.0, "unit": "1/h", "provenance": "device",
                                          "method": "STR.edf daily summary"}  # fmt: skip
    assert to_dict({"x": float("nan"), "y": 1.23456789}, CTX.tz) == {"x": None, "y": 1.235}


def test_cli_query(ingested_store, tmp_path, capsys) -> None:
    from pap_analytics import cli

    conf = tmp_path / "c.toml"
    conf.write_text(f'store_dir = "{ingested_store}"\n')
    assert cli.main(["--config", str(conf), "nights", "--days", "3", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["date"] for r in rows][-1] == "2026-11-08"
    assert cli.main(["--config", str(conf), "night", "last", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["date"] == "2026-11-08"
    assert cli.main(["--config", str(conf), "segment", "2026-11-08", "02:00", "02:30", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["channels"]["leak"]["coverage_pct"] == 100.0
    assert cli.main(["--config", str(conf), "night", "nonsense"]) == 2
    conf.write_text(f'store_dir = "{tmp_path / "none"}"\n')
    assert cli.main(["--config", str(conf), "nights"]) == 2


def _night_start(store: Store) -> datetime:
    return max(store.sessions(DEV, date(2026, 11, 8), date(2026, 11, 8)), key=lambda x: x.end - x.start).start


def test_get_signals_one_grid_no_interpolation(store: Store) -> None:
    from pap_analytics.analytics import get_signals

    a = _night_start(store) + timedelta(minutes=30)
    res = get_signals(
        store, DEV, a, a + timedelta(seconds=40), ["flow", "mask_pressure", "pressure", "pressure_hr"]
    )
    assert res.step_s == pytest.approx(0.04) and res.rows == 1000
    flow = store.channel_window(DEV, "flow", a, a + timedelta(seconds=40))[0]
    mp = store.channel_window(DEV, "mask_pressure", a, a + timedelta(seconds=40))[0]
    # fine channel: every raw sample, unchanged
    assert res.channels["flow"]["values"] == pytest.approx(flow.values[:1000].tolist())
    assert res.channels["flow"]["rate_hz"] == 25.0 and res.channels["mask_pressure"]["rate_hz"] == 0.5
    # slow channel: real samples on every 50th row (2 s / 40 ms), null elsewhere
    slow = res.channels["mask_pressure"]["values"]
    k = int((mp.start - flow.start).total_seconds() / 0.04)
    for i, v in enumerate(slow):
        if i >= k and (i - k) % 50 == 0 and (i - k) // 50 < len(mp.values):
            assert v == pytest.approx(float(mp.values[(i - k) // 50]))
        else:
            assert v is None
    assert sum(v is not None for v in slow) == len(mp.values[: (1000 - k + 49) // 50])


def test_get_signals_alias_slow_only_and_limits(store: Store) -> None:
    from pap_analytics.analytics import get_signals

    a = _night_start(store) + timedelta(minutes=30)
    # alias resolves to the canonical channel
    r = get_signals(store, DEV, a, a + timedelta(seconds=10), ["mask_pressure_hr", "pressure_hr"])
    assert list(r.channels) == ["pressure_hr"]
    # only slow channels: the grid is 2 s and a long window is allowed, nothing is aggregated
    slow = get_signals(store, DEV, a, a + timedelta(hours=1), ["mask_pressure", "leak"])
    assert slow.step_s == pytest.approx(2.0) and slow.rows == 1800
    assert all(v is not None for v in slow.channels["leak"]["values"][:1800])
    with pytest.raises(AnalyticsError, match="limited to 2000 rows"):
        get_signals(store, DEV, a, a + timedelta(minutes=5), ["flow", "pressure"])
    with pytest.raises(AnalyticsError, match="unknown"):
        get_signals(store, DEV, a, a + timedelta(seconds=5), ["zzz"])
    with pytest.raises(AnalyticsError, match="at least one"):
        get_signals(store, DEV, a, a + timedelta(seconds=5), [])


def test_get_signal_accepts_alias(store: Store) -> None:
    a = _night_start(store) + timedelta(minutes=30)
    r = get_signal(store, DEV, a, a + timedelta(minutes=1), "mask_pressure_hr", max_points=100000)
    assert r.channel == "pressure_hr" and r.bucket_s == pytest.approx(0.04)


def test_mode_switch_night_does_not_reuse_other_mode_settings(store: Store) -> None:
    """2026-11-03 ran in CPAP (see test_session_mode_from_recorded_data); the latest STR settings
    before it are ASV (2026-10-23) and must not be presented as this night's settings."""
    n = night_summary(store, DEV, date(2026, 11, 3), CTX)
    assert n.mode == "cpap"
    assert n.settings is not None and n.settings["mode"] == "asv" and n.settings["applies"] is False
    assert any("ran in mode 'cpap'" in x for x in n.notes)
    assert any("different modes" in x for x in n.notes)  # 1 min ASV fragment that evening
    assert {r["mode"] for r in n.sessions} == {None, "cpap", "asv"}
    (row,) = list_nights(store, DEV, date(2026, 11, 3), date(2026, 11, 3), CTX)
    assert row.mode == "cpap"
    n = night_summary(store, DEV, date(2026, 11, 8), CTX)
    assert n.mode == "asv" and "applies" not in n.settings
    assert not any("ran in mode" in x for x in n.notes)

def test_asv_family_session_mode_is_compatible_with_asv_auto_settings() -> None:
    """Issue #15: PLD-derived session mode 'asv' cannot distinguish ASV from ASV Auto, so STR
    settings of mode 'asv_auto' (EPAP 5-10, PS 0-6) must still apply to such a night."""
    from pap_analytics.analytics.nights import modes_compatible

    assert modes_compatible("asv", "asv_auto") and modes_compatible("asv", "asv")
    assert modes_compatible("asv", None) and modes_compatible(None, "asv_auto")
    assert not modes_compatible("asv", "cpap") and not modes_compatible("cpap", "asv_auto")
    assert not modes_compatible("apap", "asv")
