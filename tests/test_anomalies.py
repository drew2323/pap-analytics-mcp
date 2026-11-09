from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from pap_analytics.analytics import Context, find_anomalies, to_dict
from pap_analytics.analytics.timeparse import day_window
from pap_analytics.model import (
    ChannelData,
    Device,
    Event,
    EventType,
    Provenance,
    Session,
    Settings,
    therapy_day,
)
from pap_analytics.store import Store

CTX = Context()
DEV = Device("syn-1", "Syn", "Model", "1", "1", "fw", "test")
START = datetime(2026, 3, 1, 22, 0, tzinfo=UTC)  # 23:00 local (CET), therapy day 2026-03-01
HOURS = 6


def ch(name: str, unit: str, rate: float, values: np.ndarray, start: datetime = START) -> ChannelData:
    return ChannelData(name, unit, rate, start, values.astype(np.float32))


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def idx(minutes: float, rate: float) -> int:
    return int(minutes * 60 * rate)


def make_night(start: datetime = START, hours: float = HOURS, *, leak=(), events=(), desats=(),
               pressure_max=(), vent_low=(), spo2=True, tag="n") -> Session:  # fmt: skip
    """Clean night with injected faults; times in minutes from ``start``."""
    n2 = int(hours * 3600 / 2)
    leak_v = np.full(n2, 0.05)
    for a, b, v in leak:
        leak_v[idx(a, 0.5) : idx(b, 0.5)] = v
    pressure = np.full(n2, 6.0)
    for a, b in pressure_max:
        pressure[idx(a, 0.5) : idx(b, 0.5)] = 10.0
    target = np.full(n2, 6.0)
    mv = np.full(n2, 6.0)
    for a, b in vent_low:
        mv[idx(a, 0.5) : idx(b, 0.5)] = 1.5
    channels = [
        ch("leak", "L/s", 0.5, leak_v, start),
        ch("pressure", "cmH2O", 0.5, pressure, start),
        ch("minute_vent", "L/min", 0.5, mv, start),
        ch("target_vent", "L/min", 0.5, target, start),
    ]
    if spo2:
        n1 = int(hours * 3600)
        s = np.full(n1, 96.0)
        for a, depth in desats:
            i = idx(a, 1)
            for k in range(4):
                s[i + k] = 96 - depth * (k + 1) / 4
            s[i + 4 : i + 24] = 96 - depth
            for k in range(4):
                s[i + 24 + k] = 96 - depth * (3 - k) / 4
        channels.append(ch("spo2", "%", 1.0, s, start))
    evs = tuple(
        Event(start + timedelta(minutes=m), 12.0, EventType.HYPOPNEA, "Hypopnea", Provenance.DEVICE)
        for m in events
    )
    end = start + timedelta(hours=hours)
    return Session(f"syn-1:{tag}{start:%Y%m%d%H%M}", "syn-1", therapy_day(start, CTX.tz, 12), start, end,
                   True, "fp", ("x",), tuple(channels), evs)  # fmt: skip


@pytest.fixture
def make_store(tmp_path: Path):
    def build(sessions: list[Session], settings: list[Settings] | None = None) -> Store:
        st = Store.open(tmp_path / "s", read_only=False)
        st.upsert_device(DEV)
        for s in sessions:
            st.replace_session(s)
        if settings:
            st.replace_settings("syn-1", settings)
        return st

    return build


DAY = date(2026, 3, 1)


def test_clean_night_has_no_anomalies(make_store) -> None:
    with make_store([make_night(spo2=True)]) as st:
        assert find_anomalies(st, "syn-1", DAY, ctx=CTX) == []


def test_injected_faults_found_with_windows(make_store) -> None:
    night = make_night(
        leak=[(60, 66, 0.9)],
        events=[120, 122, 125, 128],
        desats=[(200, 8), (203, 8), (206, 8), (209, 8)],
        pressure_max=[(250, 262)],
        vent_low=[(300, 308)],
    )
    with make_store([night]) as st:
        res = find_anomalies(st, "syn-1", DAY, ctx=CTX)
    by = {a.kind: a for a in res}
    assert set(by) >= {"large_leak", "event_cluster", "desaturation_cluster", "low_spo2", "low_ventilation"}
    assert by["large_leak"].start == at(60) and by["large_leak"].end == at(66)
    assert by["event_cluster"].start == at(120) and by["event_cluster"].end == at(128) + timedelta(seconds=12)
    assert at(199) < by["desaturation_cluster"].start < at(201) and by["desaturation_cluster"].end < at(211)
    assert by["low_ventilation"].start == at(300) and by["low_ventilation"].end == at(308)
    assert by["low_spo2"].metrics["spo2_min"].value == 88
    # no pressure rule without known device maximum
    assert "pressure_at_max" not in by
    assert [a.severity for a in res] == sorted((a.severity for a in res), reverse=True)
    assert all(0 <= a.severity <= 1 and a.reason and a.suggested_call for a in res)
    lo, hi = night.start, night.end
    assert all(lo <= a.start <= a.end <= hi for a in res)
    assert by["large_leak"].suggested_call == "segment_stats(date='2026-03-01', start='23:59', end='00:07')"


def test_pressure_at_max_uses_settings(make_store) -> None:
    st_ = Settings("syn-1", DAY, "asv", {"epap": 6.0, "max_ps": 4.0}, {})
    with make_store([make_night(pressure_max=[(250, 262), (300, 303)])], [st_]) as st:
        res = [a for a in find_anomalies(st, "syn-1", DAY, ctx=CTX) if a.kind == "pressure_at_max"]
    assert len(res) == 1  # the 3 min span is shorter than 5 min
    assert res[0].start == at(250) and res[0].end == at(262)
    assert res[0].metrics["pressure_max"].value == 10


def test_below_thresholds_ignored(make_store) -> None:
    night = make_night(leak=[(60, 61.5, 0.9)], events=[120, 122], desats=[(200, 6), (203, 6)])
    with make_store([night]) as st:
        res = find_anomalies(st, "syn-1", DAY, ctx=CTX)
    assert {a.kind for a in res} <= {"low_spo2"}  # short leak, only 2 events, 2 desaturations


def test_thresholds_configurable(make_store) -> None:
    night = make_night(leak=[(60, 61.5, 0.9)], events=[120, 122])
    with make_store([night]) as st:
        res = find_anomalies(st, "syn-1", DAY, {"leak_min_s": 60, "cluster_min_events": 2}, CTX)
    assert {a.kind for a in res} == {"large_leak", "event_cluster"}


def test_mask_off_gap(make_store) -> None:
    a = make_night(hours=2, tag="a")
    b = make_night(start=START + timedelta(hours=2, minutes=40), hours=2, tag="b")
    c = make_night(start=START + timedelta(hours=4, minutes=45), hours=1, tag="c")
    with make_store([a, b, c]) as st:
        res = find_anomalies(st, "syn-1", DAY, ctx=CTX)
    gaps = [x for x in res if x.kind == "mask_off_gap"]
    assert len(gaps) == 1 and gaps[0].start == a.end and gaps[0].end == b.start  # 5 min gap ignored
    assert gaps[0].metrics["gap"].value == 40


def test_metric_outlier_against_previous_nights(make_store) -> None:
    nights = []
    for k in range(10):
        s = START - timedelta(days=10 - k)
        nights.append(make_night(start=s, events=[10, 50, 100 + 3 * (k % 3)] if k % 2 else [10, 50], tag="h"))
    nights.append(make_night(events=list(range(10, 340, 12)), tag="t"))  # ~28 events in 6 h
    with make_store(nights) as st:
        res = find_anomalies(st, "syn-1", DAY, ctx=CTX)
    out = [a for a in res if a.kind == "metric_outlier"]
    assert any("ahi" in a.reason and a.metrics["z"].value > 2 for a in out)
    assert all("previous 10 nights" in a.reason for a in out)


def test_cap_and_to_dict(make_store) -> None:
    leaks = [(10 + 15 * k, 13 + 15 * k, 0.9) for k in range(22)]
    with make_store([make_night(leak=leaks)]) as st:
        res = find_anomalies(st, "syn-1", DAY, ctx=CTX)
    assert len(res) == 20
    d = to_dict(res[0], CTX.tz)
    assert (
        d["kind"] == "large_leak"
        and d["start"].endswith("+01:00")
        and d["metrics"]["leak_max"]["unit"] == "L/s"
    )


def test_unknown_day_is_empty(make_store) -> None:
    with make_store([make_night()]) as st:
        assert find_anomalies(st, "syn-1", date(2025, 1, 1), ctx=CTX) == []


def test_real_2026_11_08(store: Store) -> None:
    dev = "resmed-23262018278"
    first = find_anomalies(store, dev, date(2026, 11, 8), ctx=CTX)
    assert first == find_anomalies(store, dev, date(2026, 11, 8), ctx=CTX)  # deterministic
    sessions = store.sessions(dev, date(2026, 11, 8), date(2026, 11, 8))
    lo, hi = min(s.start for s in sessions), max(s.end for s in sessions) + timedelta(seconds=1)
    dlo, dhi = day_window(date(2026, 11, 8), CTX)
    assert first and all(lo <= a.start <= a.end <= hi and dlo <= a.start <= dhi for a in first)
    spo2_hits = [a for a in first if a.kind in {"low_spo2", "desaturation_cluster"}]
    low = [a for a in first if a.kind == "low_spo2"]
    assert low and low[0].metrics["spo2_min"].value == 89  # the 89 % minimum is reported
    assert spo2_hits
    assert "mask_off_gap" in {a.kind for a in first}  # the 188 min break


def test_real_2026_10_20_many_leaks(store: Store) -> None:
    res = find_anomalies(store, "resmed-23262018278", date(2026, 10, 20), ctx=CTX)
    assert 1 <= len(res) <= 20 and any(a.kind == "large_leak" for a in res)
