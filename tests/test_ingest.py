import hashlib
import os
import shutil
import time
from datetime import date
from pathlib import Path

import pytest

from pap_analytics.config import Config, SourceConfig
from pap_analytics.ingest import ingest
from pap_analytics.store import Store

DATA = Path(__file__).parent / "data"


def cfg(tmp_path: Path, *sources: Path) -> Config:
    return Config(sources=[SourceConfig(path=s) for s in sources], store_dir=tmp_path / "store")


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.relative_to(root)}{st.st_size}{st.st_mtime_ns}".encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def test_first_second_ingest_and_source_untouched(tmp_path: Path) -> None:
    src = DATA / "resmed-air11-autoset"
    before = tree_hash(src)
    c = cfg(tmp_path, src)
    (r,) = ingest(c)
    assert (r.sessions_added, r.sessions_failed, r.errors) == (43, 0, [])
    assert r.device_id == "resmed-23262018278" and r.therapy_days == 1
    t = time.perf_counter()
    (r2,) = ingest(c)
    assert time.perf_counter() - t < 3
    assert (r2.sessions_added, r2.sessions_updated, r2.sessions_unchanged) == (0, 0, 43)
    assert tree_hash(src) == before
    with Store.open(c.store_dir, read_only=True) as s:
        assert len(s.sessions()) == 43
        last = s.last_ingest()
        assert last is not None and last.sessions_unchanged == 43


def test_touch_one_session_updates_exactly_one(tmp_path: Path) -> None:
    src = tmp_path / "card"
    shutil.copytree(DATA / "resmed-air11-autoset", src)
    c = cfg(tmp_path, src)
    ingest(c)
    pld = next(src.glob("DATALOG/20261108/20261108_233938_PLD.edf"))
    st = pld.stat()
    os.utime(pld, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    (r,) = ingest(c)
    assert (r.sessions_updated, r.sessions_added, r.sessions_unchanged) == (1, 0, 42)


def test_days_from_both_sources_are_kept(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    (r1,) = ingest(c, source=DATA / "resmed-air11-str-20261016")
    assert r1.sessions_added == 0 and r1.therapy_days == 10 and r1.errors == []
    ingest(c, source=DATA / "resmed-air11-autoset")
    with Store.open(c.store_dir, read_only=True) as s:
        dev = s.devices()[0].id
        days = [d.date for d in s.therapy_days(dev, date(2026, 1, 1), date(2026, 12, 31))]
        assert (
            days[0] == date(2026, 10, 16) and days[-1] == date(2026, 11, 8) and len(days) == 11
        )  # 10 STR records + 2026-11-08
        d18 = next(d for d in s.therapy_days(dev, date(2026, 10, 18), date(2026, 10, 18)))
        assert d18.usage_s == 60 and d18.metrics["ahi"].value is None  # 1-minute stub night
        assert len(s.settings(dev)) >= 1


def test_missing_source_files_kept_unless_pruned(tmp_path: Path) -> None:
    src = tmp_path / "card"
    shutil.copytree(DATA / "resmed-air11-autoset", src)
    c = cfg(tmp_path, src)
    ingest(c)
    shutil.rmtree(src / "DATALOG" / "20261108")
    (r,) = ingest(c)
    assert r.sessions_unchanged == 41 and r.errors == []
    with Store.open(c.store_dir, read_only=True) as s:
        assert len(s.sessions()) == 43
    ingest(c, prune=True)
    with Store.open(c.store_dir, read_only=True) as s:
        assert len(s.sessions()) == 41


def test_failed_session_does_not_stop_ingest(tmp_path: Path) -> None:
    src = tmp_path / "card"
    shutil.copytree(DATA / "resmed-air11-autoset", src)
    (src / "DATALOG/20261108/20261108_233938_PLD.edf").write_bytes(b"garbage")
    (r,) = ingest(cfg(tmp_path, src))
    assert r.sessions_failed == 1 and r.sessions_added == 42 and "20261108T223938" in r.errors[0]


def test_unknown_source_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    (r,) = ingest(cfg(tmp_path, tmp_path / "empty"))
    assert r.device_id is None and r.errors and "no adapter" in r.errors[0]


def test_rebuild(tmp_path: Path) -> None:
    c = cfg(tmp_path, DATA / "resmed-air11-str-20261016")
    ingest(c)
    (r,) = ingest(c, rebuild=True)
    assert r.errors == [] and r.therapy_days == 10


@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_cli_ingest(tmp_path: Path, flag: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    from pap_analytics import cli

    conf = tmp_path / "c.toml"
    conf.write_text(f'store_dir = "{tmp_path / "st"}"\n')
    rc = cli.main(
        ["--config", str(conf), "ingest", "--source", str(DATA / "resmed-air11-str-20261016"), *flag]
    )
    assert rc == 0
    assert "days" in capsys.readouterr().out
