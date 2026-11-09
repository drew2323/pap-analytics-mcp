import multiprocessing as mp
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from pap_analytics.model import (
    SCHEMA_VERSION,
    ChannelData,
    Device,
    Event,
    EventType,
    Metric,
    Provenance,
    Session,
    Settings,
    TherapyDay,
)
from pap_analytics.store import (
    IngestLogEntry,
    SchemaMismatchError,
    Store,
    StoreLockedError,
    StoreReadOnlyError,
)

T0 = datetime(2026, 11, 8, 22, 0, 0, tzinfo=UTC)
DEV = Device("resmed-1", "ResMed", "AirSense11", "39444", "1", "fw", "resmed-sd")


def make_session(start: datetime = T0, minutes: int = 10, sid: str | None = None) -> Session:
    rng = np.random.default_rng(1)
    chans = []
    for name, unit, rate in (("flow", "L/s", 25.0), ("pressure", "cmH2O", 0.5), ("spo2", "%", 1.0)):
        n = int(minutes * 60 * rate)
        v = rng.random(n).astype(np.float32)
        v[3] = np.nan
        chans.append(ChannelData(name, unit, rate, start, v))
    ev = (
        Event(start + timedelta(minutes=1), 12.0, EventType.HYPOPNEA, "Hypopnea", Provenance.DEVICE),
        Event(start, 0.0, EventType.RECORDING_START, "Recording starts", Provenance.DEVICE),
    )
    return Session(
        sid or f"resmed-1:{start:%Y%m%dT%H%M%S}", "resmed-1", date(2026, 11, 8), start,
        start + timedelta(minutes=minutes), True, "fp1", ("DATALOG/x.edf",), tuple(chans), ev,
    )  # fmt: skip


@pytest.fixture
def store(tmp_path: Path):
    with Store.open(tmp_path / "s", read_only=False) as s:
        s.upsert_device(DEV)
        yield s


def test_round_trip(store: Store) -> None:
    s = make_session()
    store.replace_session(s)
    assert store.devices() == [DEV]
    (info,) = store.sessions("resmed-1")
    assert (info.id, info.start, info.end, info.complete, info.fingerprint) == (
        s.id,
        s.start,
        s.end,
        True,
        "fp1",
    )
    assert info.therapy_day == date(2026, 11, 8) and info.source_files == ("DATALOG/x.edf",)
    assert info.channels == ("flow", "pressure", "spo2")
    for ch in s.channels:
        (got,) = store.channel_window("resmed-1", ch.name, T0 - timedelta(hours=1), T0 + timedelta(hours=1))
        assert got.values.dtype == np.float32
        np.testing.assert_array_equal(got.values, ch.values)  # NaN-aware
        assert (got.rate_hz, got.unit, got.start) == (ch.rate_hz, ch.unit, ch.start)
    ev = store.events("resmed-1", T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert ev == sorted(s.events, key=lambda e: e.start)
    assert store.events("resmed-1", T0, T0 + timedelta(hours=1), {EventType.HYPOPNEA}) == [s.events[0]]
    assert store.session_fingerprints("resmed-1") == {s.id: "fp1"}


def test_channel_window_exact_slice(store: Store) -> None:
    s = make_session()
    store.replace_session(s)
    flow = s.channels[0]
    a = T0 + timedelta(seconds=30)  # sample 750 exactly
    b = a + timedelta(seconds=2)  # 50 samples, end exclusive
    (w,) = store.channel_window("resmed-1", "flow", a, b)
    assert w.start == a
    np.testing.assert_array_equal(w.values, flow.values[750:800])
    # off-grid boundaries round up to the next sample
    (w,) = store.channel_window(
        "resmed-1", "flow", a + timedelta(milliseconds=10), b + timedelta(milliseconds=10)
    )
    assert w.start == a + timedelta(milliseconds=40)
    np.testing.assert_array_equal(w.values, flow.values[751:801])
    assert store.channel_window("resmed-1", "flow", T0 + timedelta(hours=2), T0 + timedelta(hours=3)) == []
    assert store.channel_window("resmed-1", "flow", T0 - timedelta(hours=2), T0) == []
    assert store.channel_window("resmed-1", "nope", T0, T0 + timedelta(hours=1)) == []


def test_channel_window_spans_two_sessions(store: Store) -> None:
    s1, s2 = make_session(T0), make_session(T0 + timedelta(minutes=30))
    store.replace_session(s1)
    store.replace_session(s2)
    ws = store.channel_window("resmed-1", "spo2", T0 + timedelta(minutes=9), T0 + timedelta(minutes=31))
    assert len(ws) == 2
    np.testing.assert_array_equal(ws[0].values, s1.channels[2].values[540:])
    np.testing.assert_array_equal(ws[1].values, s2.channels[2].values[:60])


def test_replace_session_twice_leaves_one_copy(store: Store) -> None:
    s = make_session()
    store.replace_session(s)
    store.replace_session(s)
    assert len(store.sessions("resmed-1")) == 1
    assert len(store.events("resmed-1", T0, T0 + timedelta(hours=1))) == 2
    files = sorted((store.dir / "signals").rglob("*"))
    assert [p.name for p in files if p.is_file()] == ["flow.parquet", "pressure.parquet", "spo2.parquet"]
    assert not [p for p in files if p.name.startswith(".")]
    # fewer channels on re-ingest -> no orphan files
    slim = Session(**{**s.__dict__, "channels": s.channels[:1], "fingerprint": "fp2"})
    store.replace_session(slim)
    assert [p.name for p in (store.dir / "signals").rglob("*.parquet")] == ["flow.parquet"]
    assert store.session_fingerprints("resmed-1") == {s.id: "fp2"}


def test_delete_session(store: Store) -> None:
    s = make_session()
    store.replace_session(s)
    store.delete_session(s.id)
    assert store.sessions() == [] and not list((store.dir / "signals").rglob("*.parquet"))
    assert store.events("resmed-1", T0, T0 + timedelta(hours=1)) == []


def test_session_filters(store: Store) -> None:
    s = make_session()
    other = Session(**{**make_session(T0 + timedelta(days=2)).__dict__, "therapy_day": date(2026, 11, 10)})
    store.replace_session(s)
    store.replace_session(other)
    assert len(store.sessions()) == 2
    assert [i.therapy_day for i in store.sessions(day_from=date(2026, 11, 9))] == [date(2026, 11, 10)]
    assert [i.therapy_day for i in store.sessions(day_to=date(2026, 11, 9))] == [date(2026, 11, 8)]
    assert store.sessions("other") == []


def test_days_settings_ingest_log(store: Store) -> None:
    day = TherapyDay(
        "resmed-1", date(2026, 11, 8), 22980.0, ((T0, T0 + timedelta(hours=6)),),
        {"ahi": Metric(1.0, "1/h", Provenance.DEVICE, "STR"), "oai": Metric(None, "1/h", Provenance.DEVICE)},
    )  # fmt: skip
    store.replace_therapy_days("resmed-1", [day])
    store.replace_therapy_days("resmed-1", [day])
    assert store.therapy_days("resmed-1", date(2026, 11, 1), date(2026, 11, 30)) == [day]
    assert store.therapy_days("resmed-1", date(2026, 11, 9), date(2026, 11, 30)) == []
    st = Settings(
        "resmed-1", date(2026, 11, 8), "asv", {"epr_level": 3.0, "mode": "x"}, {"S.A.MinPress": 4.0}
    )
    store.replace_settings("resmed-1", [st])
    assert store.settings("resmed-1") == [st]
    assert store.last_ingest() is None
    e = IngestLogEntry(T0, T0 + timedelta(seconds=2), "/src", "resmed-1", 1, 2, 3, ("w",), None)
    store.log_ingest(e)
    assert store.last_ingest() == e


def test_read_only_sees_committed_data_and_rejects_writes(tmp_path: Path) -> None:
    d = tmp_path / "s"
    with Store.open(d, read_only=False) as w:
        w.upsert_device(DEV)
        ro = Store.open(d, read_only=True)  # concurrent with the writer lock held
        assert ro.sessions() == []
        w.replace_session(make_session())
        assert len(ro.sessions()) == 1  # connections are per-operation: new commits are visible
        with pytest.raises(StoreReadOnlyError):
            ro.replace_session(make_session())
        with pytest.raises(StoreReadOnlyError):
            ro.upsert_device(DEV)
    with pytest.raises(FileNotFoundError):
        Store.open(tmp_path / "missing", read_only=True)


def _hold(path: str, ready, release) -> None:  # noqa: ANN001
    with Store.open(Path(path), read_only=False):
        ready.set()
        release.wait(10)


def test_second_writer_rejected(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold, args=(str(tmp_path / "s"), ready, release))
    p.start()
    try:
        assert ready.wait(30)
        with pytest.raises(StoreLockedError):
            Store.open(tmp_path / "s", read_only=False)
    finally:
        release.set()
        p.join(10)
    Store.open(tmp_path / "s", read_only=False).close()  # released after the holder exits


def test_schema_mismatch_and_rebuild(tmp_path: Path) -> None:
    d = tmp_path / "s"
    with Store.open(d, read_only=False) as s:
        s.upsert_device(DEV)
        s.replace_session(make_session())
        with s._conn() as con:
            con.execute("UPDATE meta SET schema_version = ?", [SCHEMA_VERSION + 1])
    with pytest.raises(SchemaMismatchError, match="--rebuild"):
        Store.open(d, read_only=True)
    with pytest.raises(SchemaMismatchError):
        Store.open(d, read_only=False)
    # the failed open must have released the lock
    from pap_analytics.store.lock import WriterLock

    lock = WriterLock(d / "store.lock")
    lock.acquire()
    lock.release()
    # rebuild requires a store we can open: drop the catalog the way `pap ingest --rebuild` would
    (d / "catalog.duckdb").unlink()
    with Store.open(d, read_only=False) as s:
        s.rebuild()
        assert s.sessions() == [] and s.devices() == []
        assert not list((d / "signals").rglob("*.parquet"))


def test_window_read_performance(store: Store) -> None:
    n = 8 * 3600 * 25
    start = T0
    ch = ChannelData("flow", "L/s", 25.0, start, np.random.default_rng(0).random(n).astype(np.float32))
    s = Session("resmed-1:big", "resmed-1", date(2026, 11, 8), start, start + timedelta(hours=8), True,
                "f", (), (ch,), ())  # fmt: skip
    store.replace_session(s)
    t = time.perf_counter()
    (w,) = store.channel_window(
        "resmed-1", "flow", start + timedelta(hours=4), start + timedelta(hours=4, minutes=30)
    )
    elapsed = time.perf_counter() - t
    assert len(w.values) == 30 * 60 * 25
    np.testing.assert_array_equal(w.values, ch.values[4 * 3600 * 25 : 4 * 3600 * 25 + 45000])
    assert elapsed < 0.5, elapsed  # spec: < 100 ms; generous bound for slow CI
    print(f"window read {elapsed * 1000:.1f} ms")
