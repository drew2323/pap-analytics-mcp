import contextlib
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from pap_analytics.analytics import Context, night_summary
from pap_analytics.analytics.oximetry import (
    detect_desaturations,
    filter_artefacts,
    oximetry_summary,
)
from pap_analytics.analytics.timeparse import day_window
from pap_analytics.model import ChannelData, EventType, Provenance
from pap_analytics.store import Store

DEV = "resmed-23262018278"
CTX = Context()
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def spo2(values) -> ChannelData:
    return ChannelData("spo2", "%", 1.0, T0, np.asarray(values, dtype=np.float32))


def series(n=1800, base=97.0, desats=()):
    """Flat baseline with trapezoid dips at (start_s, depth, hold_s); ramps of 3 s each side."""
    v = np.full(n, base)
    for s, depth, hold in desats:
        for k in range(3):
            v[s + k] = base - depth * (k + 1) / 3
        v[s + 3 : s + 3 + hold] = base - depth
        for k in range(3):
            v[s + 3 + hold + k] = base - depth * (2 - k) / 3
    return v


def test_known_number_of_desats() -> None:
    v = series(desats=[(100, 3, 15), (400, 4, 15), (700, 5, 20), (1000, 3, 4), (1300, 2, 30)])
    ev = detect_desaturations(spo2(v), 3.0)
    assert len(ev) == 3  # 3 %, 4 %, 5 %; the 4 s dip is too short; the 2 % dip too shallow
    assert all(e.type is EventType.DESATURATION and e.provenance is Provenance.DERIVED for e in ev)
    assert len(detect_desaturations(spo2(v), 4.0)) == 2
    assert detect_desaturations(spo2(v), 6.0) == []


def test_baseline_window_respected() -> None:
    v = np.full(600, 97.0)
    v[0:5] = 99.0  # stale high value, more than 120 s before the dips
    v[200:210] = 93.0
    v[300:400] = 94.0
    assert len(detect_desaturations(spo2(v), 3.0, baseline_window_s=120)) == 2
    # baseline is the *recent maximum*: a dip of 2 % from a 100 % peak 60 s ago counts only
    # when the window reaches back to the peak
    v2 = np.full(600, 98.0)
    v2[0:5] = 100.0
    v2[60:80] = 96.0
    assert len(detect_desaturations(spo2(v2), 3.0, baseline_window_s=120)) == 1
    assert detect_desaturations(spo2(v2), 3.0, baseline_window_s=30) == []


def test_artefact_filter() -> None:
    v = series(300)
    v[100] = 70.0  # one-sample spike: jump > 4 %/s both ways
    v[200:204] = 97.0
    v[204] = np.nan
    v[196] = np.nan  # isolated 4 s run between gaps
    f = filter_artefacts(spo2(v))
    assert np.isnan(f[100]) and np.isnan(f[200:204]).all()
    assert np.isfinite(f[102:196]).all()
    # a spike would otherwise create a fake desaturation
    assert detect_desaturations(spo2(f), 3.0) == []
    # 3 %/s ramp (<4) survives
    ramp = filter_artefacts(spo2(series(60, desats=[(10, 9, 20)])))
    assert np.isfinite(ramp[10:60]).all()


class FakeStore:
    def __init__(self, ch, pulse=None, events=()):
        self.ch, self.pulse, self._events = ch, pulse, events

    def channel_window(self, device_id, name, start, end):
        c = self.ch if name == "spo2" else self.pulse
        return [c] if c is not None else []

    def events(self, device_id, start, end, types=None):
        return list(self._events)

    def connection(self):  # the real Store shares one catalog connection; nothing to share here
        return contextlib.nullcontext()


def test_summary_synthetic_exact_odi() -> None:
    v = series(3600, desats=[(300, 3, 15), (900, 4, 15), (1500, 4, 15), (2100, 5, 20)])
    pulse = ChannelData("pulse", "1/min", 1.0, T0, np.full(3600, 60.0, dtype=np.float32))
    r = oximetry_summary(FakeStore(spo2(v), pulse), DEV, T0, T0 + timedelta(hours=1))
    assert r is not None and r.recorded_s == 3600 and r.valid_s == 3600
    m = r.metrics
    assert m["odi3"].value == pytest.approx(4.0) and m["odi4"].value == pytest.approx(3.0)
    assert m["spo2_min"].value == 92 and m["pulse_mean"].value == 60 and m["pulse_max"].value == 60
    assert m["t90_min"].value == 0 and all(x.provenance is Provenance.DERIVED for x in m.values())
    assert len(r.desaturations) == 4 and r.desaturations[0]["depth"] == 3


def test_summary_none_without_valid_spo2() -> None:
    assert oximetry_summary(FakeStore(spo2(np.full(100, np.nan))), DEV, T0, T0 + timedelta(hours=1)) is None


def test_t90_and_association() -> None:
    from pap_analytics.model import Event

    v = series(1200, base=96.0, desats=[(300, 7, 60)])
    ev = [Event(T0 + timedelta(seconds=290), 10.0, EventType.HYPOPNEA, "Hypopnea", Provenance.DEVICE)]
    r = oximetry_summary(FakeStore(spo2(v), None, ev), DEV, T0, T0 + timedelta(hours=1))
    assert r is not None
    assert r.metrics["t90_min"].value == pytest.approx(1.0, abs=0.1)  # samples < 90 for ~60 s
    assert r.metrics["desat_after_event_pct"].value == 100.0  # starts 300–302 s, event ended 300 s
    assert "no valid pulse data" in r.notes


def _loop_odi(values, drop, window=120, min_dur=10):
    """Deliberately naive, independent re-implementation (no shared code)."""
    v = list(values)
    n, count, i = len(v), 0, 0
    while i < n:
        if v[i] != v[i]:
            i += 1
            continue
        best = None
        for k in range(max(0, i - window), i):
            if v[k] == v[k] and (best is None or v[k] > best):
                best = v[k]
        if best is not None and v[i] <= best - drop:
            j = i
            while j < n and v[j] == v[j] and v[j] <= best - drop:
                j += 1
            if j - i >= min_dur:
                count += 1
            i = j
        else:
            i += 1
    return count


def test_real_2026_11_08_and_independent_odi(store: Store) -> None:
    lo, hi = day_window(date(2026, 11, 8), CTX)
    r = oximetry_summary(store, DEV, lo, hi)
    assert r is not None and r.recorded_s == 22860
    raw = store.channel_window(DEV, "spo2", lo, hi)
    assert sum(int(np.isfinite(p.values).sum()) for p in raw) == 22549  # documented valid seconds
    assert min(float(np.nanmin(p.values)) for p in raw if np.isfinite(p.values).any()) == 89
    assert r.metrics["spo2_min"].value == 89  # the slope filter does not change the minimum here
    assert 0 < r.metrics["t90_min"].value < 5
    assert r.valid_s <= 22549 and r.metrics["valid_pct"].value > 90
    filtered = [filter_artefacts(p) for p in raw]
    valid_h = sum(int(np.isfinite(f).sum()) for f in filtered) / 3600
    for drop, key in ((3, "odi3"), (4, "odi4")):
        expected = sum(_loop_odi(f, drop) for f in filtered) / valid_h
        assert r.metrics[key].value == pytest.approx(expected)
        assert r.metrics[key].value < 15
    assert "pulse_mean" in r.metrics and r.metrics["pulse_min"].value > 30


def test_no_valid_spo2_day_is_none_with_note(store: Store) -> None:
    lo, hi = day_window(date(2026, 11, 4), CTX)
    assert oximetry_summary(store, DEV, lo, hi) is None
    n = night_summary(store, DEV, date(2026, 11, 4), CTX)
    assert n.oximetry is None and any("SpO2" in x for x in n.notes)


def test_night_summary_includes_oximetry(store: Store) -> None:
    n = night_summary(store, DEV, date(2026, 11, 8), CTX)
    assert n.oximetry is not None and n.oximetry["metrics"]["spo2_min"].value == 89
    assert "odi3" in n.oximetry["metrics"]


def test_thresholds_override(store: Store) -> None:
    lo, hi = day_window(date(2026, 11, 8), CTX)
    loose = oximetry_summary(store, DEV, lo, hi, {"spo2_max_slope": 100, "min_valid_run_s": 0})
    assert loose is not None and loose.valid_s == 22549
