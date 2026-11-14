"""Vectorised flow shape / desaturations equal the v0.1.7 loops; trend cache never changes results."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
from loops_v017 import breath_shapes_loop, detect_loop

from pap_analytics.analytics import Context
from pap_analytics.analytics import trend_cache as tc
from pap_analytics.analytics.breathing import segment_breaths
from pap_analytics.analytics.disturbances import disturbance_trend, refresh_trend_cache
from pap_analytics.analytics.flowshape import GLASGOW, breath_shapes
from pap_analytics.analytics.oximetry import _detect, filter_artefacts
from pap_analytics.analytics.timeparse import day_window
from pap_analytics.model import ChannelData
from pap_analytics.store import Store

DEV = "resmed-23262018278"
T0 = datetime(2026, 1, 1, tzinfo=UTC)

def assert_same_shapes(f: ChannelData) -> int:
    b = segment_breaths(f)
    a, c = breath_shapes_loop(f, b), breath_shapes(f, b)
    assert np.array_equal(a.valid, c.valid)
    for k in ("qpeak_lpm", "ned_pct", "fi"):
        np.testing.assert_allclose(getattr(c, k), getattr(a, k), rtol=1e-12, atol=1e-9, equal_nan=True)
    for k in GLASGOW:
        assert np.array_equal(a.glasgow[k], c.glasgow[k]), k
    return len(b.start_s)

def test_vectorised_flow_shape_equals_loop_on_real_nights(store: Store) -> None:
    n = 0
    for d in (date(2026, 10, 16), date(2026, 11, 3), date(2026, 11, 8)):  # ASV, CPAP + truncated, ASV
        lo, hi = day_window(d, Context())
        for p in store.channel_window(DEV, "flow", lo, hi):
            n += assert_same_shapes(p)
    assert n > 10000

def test_vectorised_flow_shape_equals_loop_on_random_and_edge_signals() -> None:
    rng = np.random.default_rng(0)
    for _ in range(20):
        t = np.arange(20000) / 25
        v = np.sin(2 * np.pi * t / rng.uniform(2, 6)) * rng.uniform(0.1, 1)
        v += 0.3 * np.cumsum(rng.normal(0, 0.05, len(t))) + rng.normal(0, 0.03, len(t))
        assert_same_shapes(ChannelData("flow", "L/s", 25.0, T0, v.astype(np.float32)))
    for v in (np.zeros(500), np.full(500, np.nan)):
        assert_same_shapes(ChannelData("flow", "L/s", 25.0, T0, v.astype(np.float32)))

def test_vectorised_desaturations_equal_loop(store: Store) -> None:
    args = [(3.0, 10.0, 120.0), (4.0, 10.0, 120.0), (3.0, 0.0, 30.0), (2.0, 5.0, 1.0)]
    events = 0
    for d in (date(2026, 10, 20), date(2026, 11, 8)):
        lo, hi = day_window(d, Context())
        for p in store.channel_window(DEV, "spo2", lo, hi):
            for v in (p.values.astype(np.float64), filter_artefacts(p, 4.0, 10.0)):
                for a in args:
                    ref = detect_loop(v, p.rate_hz, *a)
                    assert _detect(v, p.rate_hz, *a) == ref
                    events += len(ref)
    assert events > 10
    rng = np.random.default_rng(1)
    for _ in range(100):
        v = 95 + np.cumsum(rng.normal(0, 0.6, 3000))
        v[rng.random(3000) < 0.05] = np.nan
        for a in [*args, (3.0, 10.0, 0.5)]:
            assert _detect(v, 1.0, *a) == detect_loop(v, 1.0, *a)
    assert _detect(np.empty(0), 1.0, 3, 10, 120) == []

# ---- cache ---------------------------------------------------------------------------------------

def dumps(t: dict) -> str:
    return json.dumps({k: v for k, v in t.items() if k != "cached_nights"}, default=str, sort_keys=True)

@pytest.fixture()
def cached_store(ingested_store: Path, tmp_path: Path):
    """Copy of the ingested fixture store (the cache file is written next to the catalog)."""
    import shutil

    d = tmp_path / "store"
    shutil.copytree(ingested_store, d, ignore=shutil.ignore_patterns("store.lock", tc.CACHE_FILE))
    with Store.open(d, read_only=True) as s:
        yield s

DAYS = (date(2026, 11, 3), date(2026, 11, 8))

def test_cache_gives_identical_results_and_is_used(cached_store: Store) -> None:
    s, ctx = cached_store, Context()
    live = disturbance_trend(s, DEV, *DAYS, ctx, use_cache=False)
    assert live["cached_nights"] == 0
    computed = refresh_trend_cache(s, ctx)
    assert computed == len({x.therapy_day for x in s.sessions(DEV)})  # every night of the store, once
    assert refresh_trend_cache(s, ctx) == 0  # nothing stale
    cached = disturbance_trend(s, DEV, *DAYS, ctx)
    assert cached["cached_nights"] == len(live["nights"]) > 0
    assert dumps(cached) == dumps(live)

def test_cache_key_changes_with_sessions_thresholds_and_version(cached_store: Store, monkeypatch) -> None:
    s, ctx = cached_store, Context()
    sess = s.sessions(DEV, DAYS[1], DAYS[1])
    k = tc.night_key(sess, ctx)
    assert tc.night_key(sess, ctx) == k
    assert tc.night_key([replace(sess[0], fingerprint="x"), *sess[1:]], ctx) != k
    assert tc.night_key([replace(sess[0], complete=False), *sess[1:]], ctx) != k
    assert tc.night_key(sess, Context(thresholds={"ned_fl_pct": 30})) != k
    assert tc.night_key(sess, Context(boundary_hour=11)) != k
    monkeypatch.setattr(tc, "__version__", "9.9.9")
    assert tc.night_key(sess, ctx) != k

def test_stale_foreign_or_corrupt_cache_falls_back_to_live(cached_store: Store) -> None:
    s, ctx = cached_store, Context()
    live = disturbance_trend(s, DEV, *DAYS, ctx, use_cache=False)
    refresh_trend_cache(s, ctx)
    path = s.dir / tc.CACHE_FILE
    data = json.loads(path.read_text())
    for entry in data["devices"][DEV].values():
        entry["row"]["glasgow_index"] = -1.0  # poisoned values ...
        entry["key"] = "stale"  # ... behind a stale key must never be returned
    path.write_text(json.dumps(data))
    t = disturbance_trend(s, DEV, *DAYS, ctx)
    assert t["cached_nights"] == 0 and dumps(t) == dumps(live)
    # different thresholds: entries keyed for the defaults are not used
    t2 = disturbance_trend(s, DEV, *DAYS, Context(thresholds={"ned_fl_pct": 30}))
    assert t2["cached_nights"] == 0
    for content in ("{not json", json.dumps({"format": 999, "devices": {}}), json.dumps([1, 2])):
        path.write_text(content)
        t = disturbance_trend(s, DEV, *DAYS, ctx)
        assert t["cached_nights"] == 0 and dumps(t) == dumps(live)
    path.unlink()
    assert dumps(disturbance_trend(s, DEV, *DAYS, ctx)) == dumps(live)

def test_cli_ingest_writes_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from pap_analytics import cli

    conf = tmp_path / "c.toml"
    conf.write_text(f'store_dir = "{tmp_path / "st"}"\n')
    src = Path(__file__).parent / "data" / "resmed-air11-str-20261016"
    assert cli.main(["--config", str(conf), "ingest", "--source", str(src)]) == 0
    assert (tmp_path / "st" / tc.CACHE_FILE).exists()  # STR-only source: no sessions, empty cache
    assert tc.read(tmp_path / "st") == {"resmed-23262018278": {}}
