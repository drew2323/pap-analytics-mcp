from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from edf_synth import make_edf

from pap_analytics.edf import EdfFile

DL = "DATALOG/20261108"


def test_all_real_files_open(resmed_air11: Path, resmed_air11_str_history: Path) -> None:
    files = sorted(resmed_air11.rglob("*.edf"))
    assert len(files) == 216
    for f in files:
        edf = EdfFile.open(f)
        # only documented truncations (partial trailing record in BRP) are allowed
        assert all(w.startswith("truncated") for w in edf.warnings), (f.name, edf.warnings)
        assert edf.verify_crc() == [], f.name
    EdfFile.open(resmed_air11_str_history / "STR.edf")


def test_pld_headers(resmed_air11: Path) -> None:
    edf = EdfFile.open(resmed_air11 / DL / "20261108_233938_PLD.edf")
    assert list(edf.signals) == [
        "MaskPress.2s", "Press.2s", "EprPress.2s", "Leak.2s", "RespRate.2s", "TidVol.2s",
        "MinVent.2s", "TgtVent.2s", "IERatio.2s", "Snore.2s", "FlowLim.2s", "Ti.2s",
    ]  # fmt: skip
    assert "Crc16" not in edf.signals and "Crc16" in edf.raw_signal_headers
    assert len(edf.signals) == 12
    assert edf.record_duration_s == 60
    assert edf.signals["Leak.2s"].rate_hz == 0.5 and edf.signals["Leak.2s"].unit == "L/s"
    assert edf.signals["TidVol.2s"].unit == "L"
    assert not edf.is_edf_plus
    assert edf.start.year == 2026 and (edf.start.month, edf.start.day) == (11, 8)


def test_brp_flow(resmed_air11: Path) -> None:
    edf = EdfFile.open(resmed_air11 / DL / "20261108_233938_BRP.edf")
    h = edf.signals["Flow.40ms"]
    assert h.rate_hz == 25
    v = edf.read_signal("Flow.40ms")
    assert v.dtype == np.float32
    assert len(v) == edf.n_records * 25 * 60
    assert v.min() >= h.phys_min - 1e-3 and v.max() <= h.phys_max + 1e-3


def test_sa2_spo2_matches_readme(resmed_air11: Path) -> None:
    total = valid = 0
    low = 100.0
    for f in sorted((resmed_air11 / DL).glob("*_SA2.edf")):
        s = EdfFile.open(f).read_signal("SpO2.1s")
        total += len(s)
        ok = (s >= 50) & (s <= 100)
        valid += int(ok.sum())
        if ok.any():
            low = min(low, float(s[ok].min()))
    assert (valid, total, low) == (22549, 22860, 89)


def test_eve_annotations_over_whole_set(resmed_air11: Path) -> None:
    from collections import Counter

    c: Counter[str] = Counter()
    for f in sorted(resmed_air11.rglob("*_EVE.edf")):
        edf = EdfFile.open(f)
        assert edf.is_edf_plus
        c.update(a.text for a in edf.annotations())
        assert not any("unparseable" in w for w in edf.warnings)
    assert dict(c) == {"Hypopnea": 129, "Apnea": 19, "Central Apnea": 1, "Arousal": 1, "Recording starts": 43}


def test_eve_annotation_times(resmed_air11: Path) -> None:
    edf = EdfFile.open(resmed_air11 / DL / "20261108_233938_EVE.edf")
    anns = edf.annotations()
    assert anns[0].text == "Recording starts" and anns[0].onset_s == 0
    hyp = anns[1]
    assert hyp.text == "Hypopnea" and hyp.onset_s == 4200 and hyp.duration_s == 0
    assert hyp.start == edf.start + timedelta(seconds=4200)
    ap = next(a for a in anns if a.text == "Apnea")
    assert ap.onset_s == 18355 and ap.duration_s == 10


def test_str_record_counts(resmed_air11: Path, resmed_air11_str_history: Path) -> None:
    assert EdfFile.open(resmed_air11 / "STR.edf").n_records == 1
    assert EdfFile.open(resmed_air11_str_history / "STR.edf").n_records == 10


def test_synth_truncated_last_record() -> None:
    recs = [[0, 1000, 2000, 3000]] * 3
    data = make_edf(signals=[("A", recs)], truncate_bytes=3)
    p = _write(data)
    edf = EdfFile.open(p)
    assert edf.n_records == 2
    assert any("declared 3" in w for w in edf.warnings)
    assert len(edf.read_signal("A")) == 8


def test_synth_unknown_record_count_and_scaling() -> None:
    edf = EdfFile.open(_write(make_edf(signals=[("A", [[0, 32767]] * 2)], declared_records=-1)))
    assert edf.n_records == 2 and edf.warnings == []
    np.testing.assert_allclose(edf.read_signal("A"), [0, 3276.7, 0, 3276.7], rtol=1e-6)


@pytest.mark.parametrize(
    ("yy", "year"), [("84", 2084), ("85", 1985), ("99", 1999), ("00", 2000), ("26", 2026)]
)
def test_synth_two_digit_year(yy: str, year: int) -> None:
    edf = EdfFile.open(_write(make_edf(signals=[("A", [[1]])], start=f"01.02.{yy} 03.04.05")))
    assert (edf.start.year, edf.start.month, edf.start.day, edf.start.hour) == (year, 2, 1, 3)


def test_synth_edf_plus_startdate_overrides() -> None:
    data = make_edf(
        signals=[("A", [[1]])], start="01.02.26 03.04.05", recording="Startdate 15-MAR-2031 X X X"
    )
    assert EdfFile.open(_write(data)).start.year == 2031


def test_synth_multi_text_tal() -> None:
    tal = b"+0\x14\x14\x00+10\x1512.5\x14Foo\x14Bar\x14\x00"
    edf = EdfFile.open(_write(make_edf(tals=[tal], edf_plus=True)))
    anns = edf.annotations()
    assert [(a.text, a.onset_s, a.duration_s) for a in anns] == [("Foo", 10, 12.5), ("Bar", 10, 12.5)]


_n = 0
_tmp: Path


@pytest.fixture(autouse=True)
def _tmpdir(tmp_path: Path) -> None:
    global _tmp
    _tmp = tmp_path


def _write(data: bytes) -> Path:
    global _n
    _n += 1
    p = _tmp / f"s{_n}.edf"
    p.write_bytes(data)
    return p
