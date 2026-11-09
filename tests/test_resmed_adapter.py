from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from pap_analytics import adapters
from pap_analytics.adapters.resmed import ResmedAdapter
from pap_analytics.adapters.resmed.identification import parse_json, parse_tgt
from pap_analytics.adapters.resmed.mapping import mode_name
from pap_analytics.model import EventType

TZ = ZoneInfo("Europe/Prague")


@pytest.fixture(scope="module")
def adapter() -> ResmedAdapter:
    # fixed far-future clock: checked-out fixture files have fresh mtimes
    return ResmedAdapter(clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))


def test_detect_and_device(resmed_air11: Path, resmed_air11_str_history: Path) -> None:
    assert adapters.detect(resmed_air11).name == "resmed"
    assert adapters.detect(resmed_air11_str_history).name == "resmed"  # STR-only snapshot, no DATALOG
    d = adapters.get("resmed").device(resmed_air11)
    assert (d.model, d.model_code, d.serial, d.id) == (
        "AirSense11AutoSet", "39444", "23262018278", "resmed-23262018278",
    )  # fmt: skip
    assert d.vendor == "ResMed" and d.source_kind == "resmed-sd"


def test_tgt_parser() -> None:
    d = parse_tgt("#SRN 22201234567\n#PNA AirSense_10_AutoSet\n#PCD 37028\n")
    assert (d.id, d.model, d.model_code) == ("resmed-22201234567", "AirSense_10_AutoSet", "37028")


def test_json_parser_needs_serial() -> None:
    with pytest.raises(ValueError):
        parse_json('{"FlowGenerator":{"IdentificationProfiles":{"Product":{}}}}')


def test_scan_sessions(adapter: ResmedAdapter, resmed_air11: Path) -> None:
    refs = adapter.scan(resmed_air11, TZ, 12)
    assert len(refs) == 43
    assert len({r.id for r in refs}) == 43
    d8 = [r for r in refs if r.therapy_day == date(2026, 11, 8)]
    assert [r.files[0].name[9:15] for r in d8] == ["203013", "233938"]
    assert all(len(r.fingerprint) == 64 for r in refs)


def test_folder_name_is_noon_to_noon_day(adapter: ResmedAdapter, resmed_air11: Path) -> None:
    """Folder 20261026 holds files stamped 20261027 00:36/05:45; they belong to therapy day 2026-10-26."""
    refs = adapter.scan(resmed_air11, TZ, 12)
    for folder, day in (("20261026", date(2026, 10, 26)), ("20261028", date(2026, 10, 28))):
        in_folder = [r for r in refs if r.files[0].parent.name == folder]
        assert in_folder and all(r.therapy_day == day for r in in_folder)
    r = next(r for r in refs if r.files[0].name.startswith("20261027_003639"))
    assert r.start == datetime(2026, 10, 26, 23, 36, 39, tzinfo=UTC)  # CET (UTC+1) after DST end on 25th
    assert adapter.scan(resmed_air11.parent / "resmed-air11-str-20261016", TZ, 12) == []


def test_load_session_20261108(adapter: ResmedAdapter, resmed_air11: Path) -> None:
    ref = next(r for r in adapter.scan(resmed_air11, TZ, 12) if r.files[0].name.startswith("20261108_233938"))
    s = adapter.load_session(resmed_air11, ref, TZ, 12)
    assert s.start == datetime(2026, 11, 8, 22, 39, 38, tzinfo=UTC)
    assert s.id == "resmed-23262018278:20261108T223938"
    by = {c.name: c for c in s.channels}
    assert {"flow", "pressure", "pressure_hr", "spo2", "pulse", "tidal_volume", "leak"} <= set(by)
    assert by["flow"].rate_hz == 25 and by["pressure"].rate_hz == 0.5 and by["spo2"].rate_hz == 1
    assert all(c.values.dtype == np.float32 and c.start == s.start for c in s.channels)
    assert 300 < float(np.nanmedian(by["tidal_volume"].values)) < 800  # mL, not L
    assert by["tidal_volume"].unit == "mL"
    spo2 = by["spo2"].values
    # README: 22549 valid seconds on the day in total; subtract the other session's share
    assert int(np.isfinite(spo2).sum()) == 22549 - _valid_other_session(adapter, resmed_air11)
    assert np.nanmin(spo2) == 89
    assert s.end - s.start == timedelta(minutes=380)
    counts: dict[EventType, int] = {}
    for e in s.events:
        counts[e.type] = counts.get(e.type, 0) + 1
    assert counts[EventType.RECORDING_START] == 1 and counts[EventType.APNEA] == 1
    assert counts[EventType.HYPOPNEA] == 6
    assert s.complete  # fixture files are old
    assert adapter.warnings == []


def _valid_other_session(adapter: ResmedAdapter, root: Path) -> int:
    ref = next(r for r in adapter.scan(root, TZ, 12) if r.files[0].name.startswith("20261108_203013"))
    s = adapter.load_session(root, ref, TZ, 12)
    return int(np.isfinite(next(c for c in s.channels if c.name == "spo2").values).sum())


def test_all_events_match_eve_counts(adapter: ResmedAdapter, resmed_air11: Path) -> None:
    total: dict[EventType, int] = {}
    for ref in adapter.scan(resmed_air11, TZ, 12):
        for e in adapter.load_session(resmed_air11, ref, TZ, 12).events:
            total[e.type] = total.get(e.type, 0) + 1
    assert total == {
        EventType.HYPOPNEA: 129, EventType.APNEA: 19, EventType.CENTRAL_APNEA: 1,
        EventType.AROUSAL: 1, EventType.RECORDING_START: 43,
    }  # fmt: skip


def test_complete_flag_for_growing_newest_session(resmed_air11: Path) -> None:
    refs = ResmedAdapter().scan(resmed_air11, TZ, 12)
    far_future = lambda: datetime(2030, 1, 1, tzinfo=UTC)  # noqa: E731
    just_written = lambda: datetime.now(UTC)  # noqa: E731
    assert ResmedAdapter(clock=far_future).load_session(resmed_air11, refs[-1], TZ, 12).complete
    a = ResmedAdapter(clock=just_written, growing_window=timedelta(days=36500))
    assert not a.load_session(resmed_air11, refs[-1], TZ, 12).complete
    assert a.load_session(resmed_air11, refs[0], TZ, 12).complete  # only the newest can grow


def test_str_history(adapter: ResmedAdapter, resmed_air11_str_history: Path) -> None:
    days = {d.date: d for d in adapter.therapy_days(resmed_air11_str_history, TZ)}
    assert sorted(days) == [date(2026, 10, d) for d in (16, 17, 18, 19, 20, 21, 22, 23, 24, 25)][: len(days)]
    assert len(days) == 10
    # (usage min, AHI, HI, AI, leak_p95 L/s, mask_pressure_p95) from tests/data/README.md
    ref = {
        date(2026, 10, 16): (349, 1.2, 1.0, 0.1, 0.72, 6.72),
        date(2026, 10, 17): (434, 0.4, 0.4, 0.0, 0.74, 6.72),
        date(2026, 10, 19): (419, 2.0, 1.8, 0.1, 0.62, 6.72),
        date(2026, 10, 20): (362, 4.3, 3.4, 0.8, 0.64, 6.56),
        date(2026, 10, 21): (430, 2.9, 2.5, 0.4, 0.82, 6.88),
        date(2026, 10, 22): (435, 4.6, 3.8, 0.8, 0.56, 6.72),
        date(2026, 10, 23): (390, 1.5, 1.3, 0.1, 0.24, 5.76),
        date(2026, 10, 24): (404, 0.4, 0.2, 0.1, 0.26, 5.60),
        date(2026, 10, 25): (355, 0.5, 0.5, 0.0, 0.14, 5.60),
    }  # fmt: skip
    for day, (mins, ahi, hi, ai, leak, mp) in ref.items():
        d = days[day]
        assert d.usage_s == mins * 60
        m = d.metrics
        assert (m["ahi"].value, m["hi"].value, m["ai"].value) == pytest.approx((ahi, hi, ai))
        assert m["leak_p95"].value == pytest.approx(leak) and m["mask_pressure_p95"].value == pytest.approx(
            mp
        )
        assert m["ahi"].provenance.value == "device"
    assert days[date(2026, 10, 16)].metrics["oai"].value is None  # -0.1 = not available
    assert days[date(2026, 10, 16)].metrics["spo2_p50"].value is None  # STR carries no oximetry


def test_str_main_set(adapter: ResmedAdapter, resmed_air11: Path) -> None:
    (day,) = adapter.therapy_days(resmed_air11, TZ)
    assert day.date == date(2026, 11, 8) and day.usage_s == 383 * 60
    assert day.metrics["ahi"].value == 1.0 and day.metrics["leak_p95"].value == pytest.approx(0.14)
    assert day.metrics["tidal_volume_p50"].unit == "mL"
    # mask on 20261108 21:30?? -> intervals are minutes from local noon; first = 19:30Z..19:32Z
    assert day.mask_intervals[-1][0] == datetime(2026, 11, 8, 22, 39, tzinfo=UTC)
    (st,) = adapter.settings(resmed_air11, TZ)
    assert st.valid_from == date(2026, 11, 8)
    assert st.mode == "asv"  # Mode 6 = ASV on the author's firmware
    assert st.raw["S.A.MinPress"] == 4 and st.raw["S.A.MaxPress"] == 20
    assert st.values["epr_level"] == 3 and st.values["epap"] == 4 and st.values["max_ps"] == 3


def test_settings_history_collapses_identical_days(
    adapter: ResmedAdapter, resmed_air11_str_history: Path
) -> None:
    st = adapter.settings(resmed_air11_str_history, TZ)
    assert 1 <= len(st) <= 10 and st[0].valid_from == date(2026, 10, 16)


def test_mode_names() -> None:
    assert mode_name(6) == "asv" and mode_name(1) == "apap" and mode_name(None) is None
    assert mode_name(99) == "unknown_99"


def test_unknown_label_and_event_become_warnings(tmp_path: Path) -> None:
    import shutil

    from edf_synth import make_edf

    root = tmp_path / "card"
    day = root / "DATALOG" / "20261108"
    day.mkdir(parents=True)
    src = Path(__file__).parent / "data" / "resmed-air11-autoset"
    shutil.copy(src / "Identification.json", root)
    shutil.copy(src / "STR.edf", root)
    (day / "20261108_203013_PLD.edf").write_bytes(
        make_edf(signals=[("Mystery.2s", [[1, 2]]), ("Leak.2s", [[0, 100]])], record_duration=4.0)
    )
    tal = b"+0\x14\x14\x00+5\x150\x14Weird Event\x14\x00"
    (day / "20261108_203013_EVE.edf").write_bytes(make_edf(tals=[tal], edf_plus=True))
    a = ResmedAdapter(clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    (ref,) = a.scan(root, TZ, 12)
    s = a.load_session(root, ref, TZ, 12)
    assert [c.name for c in s.channels] == ["leak"]
    assert [e.type for e in s.events] == [EventType.OTHER] and s.events[0].raw_type == "Weird Event"
    assert any("Mystery.2s" in w for w in a.warnings) and any("Weird Event" in w for w in a.warnings)
