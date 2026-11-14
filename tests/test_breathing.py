import contextlib
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from pap_analytics.analytics import Context
from pap_analytics.analytics.breathing import (
    CALM,
    RESTLESS,
    UNKNOWN,
    breath_ratio_before,
    epoch_irregularity,
    epoch_states,
    segment_breaths,
)
from pap_analytics.analytics.disturbances import disturbance_summary, night_disturbances
from pap_analytics.analytics.timeparse import day_window
from pap_analytics.model import ChannelData
from pap_analytics.store import Store

DEV = "resmed-23262018278"
CTX = Context()
T0 = datetime(2026, 1, 1, tzinfo=UTC)
RATE = 25.0

def sine_breaths(periods, amps) -> np.ndarray:
    """Concatenated full sine cycles (inspiration = positive half)."""
    out = []
    for per, amp in zip(periods, amps, strict=True):
        t = np.arange(int(round(per * RATE))) / RATE
        out.append(amp * np.sin(2 * np.pi * t / per))
    return np.concatenate(out)

def flow(values) -> ChannelData:
    return ChannelData("flow", "L/s", RATE, T0, np.asarray(values, dtype=np.float32))

def test_segmentation_of_regular_breathing() -> None:
    v = sine_breaths([4.0] * 100, [0.5] * 100)
    b = segment_breaths(flow(v))
    # every inspiration start except the first (no preceding expiration) and the last (no next start)
    assert len(b.start_s) == 98
    assert np.allclose(b.period_s, 4.0, atol=0.05)
    # tidal volume = integral of 0.5 sin over the 2 s half period = 0.5 * 2 * 2 / pi L = 637 mL
    # (the 0.2 s smoothing and the +/-0.03 L/s hysteresis cut a few mL)
    assert np.median(b.vt_ml) == pytest.approx(1000 * 0.5 * 2 * 2 / np.pi, rel=0.03)

def test_regular_breathing_is_calm_and_variable_block_restless() -> None:
    rng = np.random.default_rng(0)
    n = 300  # 4 s breaths -> 1200 s -> 40 epochs
    periods = np.full(n, 4.0)
    amps = np.full(n, 0.5)
    block = slice(100, 175)  # breaths 100..174 = 400..700 s
    periods[block] = rng.uniform(2.5, 6.0, 75)
    amps[block] = rng.uniform(0.15, 0.9, 75)
    b = segment_breaths(flow(sine_breaths(periods, amps)))
    starts = np.concatenate(([0.0], np.cumsum(periods)))
    irr = epoch_irregularity(b, starts[-1])
    states = epoch_states(irr, 0.3)
    assert all(s == CALM for s in states[2:10]) and all(s == CALM for s in states[27:38])
    restless = [i for i, s in enumerate(states) if s == RESTLESS]
    # block 400..700 s; the 2 min window reaches 45 s before / 75 s after an epoch, so restless
    # epochs lie within [400 - 75, 700 + 45) s and cover the block's core
    assert restless and restless[0] * 30 >= 400 - 75 and (restless[-1] + 1) * 30 <= 700 + 45
    assert set(range(15, 22)) <= set(restless)

def test_missing_flow_is_unknown() -> None:
    v = sine_breaths([4.0] * 150, [0.5] * 150)
    v[int(200 * RATE) : int(400 * RATE)] = np.nan  # 200 s without flow
    b = segment_breaths(flow(v))
    states = epoch_states(epoch_irregularity(b, 600), 0.3)
    # epochs whose 2 min window lies entirely in the gap have too few breaths
    assert states[8:11] == [UNKNOWN] * 3 and states[0:5] == [CALM] * 5

def test_isolated_epoch_flip_is_unknown() -> None:
    irr = np.array([0.1, 0.5, 0.1, 0.1, 0.5, 0.5, np.nan])
    assert epoch_states(irr, 0.3) == [CALM, UNKNOWN, CALM, CALM, RESTLESS, RESTLESS, UNKNOWN]

def test_breath_ratio_before() -> None:
    amps = np.full(60, 0.5)
    amps[45:47] = 0.1  # two small breaths at 180..188 s
    b = segment_breaths(flow(sine_breaths([4.0] * 60, amps)))
    r = breath_ratio_before(b, 200.0)
    assert r == pytest.approx(0.2, abs=0.03)  # 0.1 / 0.5 amplitude
    assert breath_ratio_before(b, 50.0) is None  # baseline window starts before the recording

class FakeStore:
    def __init__(self, channels, events=()):
        self.channels, self._events = channels, list(events)

    def channel_window(self, device_id, name, start, end):
        return [self.channels[name]] if name in self.channels else []

    def events(self, device_id, start, end, types=None):
        return list(self._events)

    def connection(self):  # the real Store shares one catalog connection; nothing to share here
        return contextlib.nullcontext()

def test_pulse_rise_breathing_response_links() -> None:
    n = 3600
    pulse = np.full(n, 60.0)
    tv = np.full(n // 2, 400.0)  # 0.5 Hz device tidal volume
    for at in (1000, 2000):
        pulse[at : at + 12] = [65, 70, 75, 75, 75, 75, 75, 75, 75, 75, 70, 65]
    tv[1001 // 2 + 3] = 700.0  # 1.75 x baseline 6 s after the first rise -> recovery breath
    store = FakeStore({
        "spo2": ChannelData("spo2", "%", 1.0, T0, np.full(n, 97.0, dtype=np.float32)),
        "pulse": ChannelData("pulse", "1/min", 1.0, T0, pulse.astype(np.float32)),
        "tidal_volume": ChannelData("tidal_volume", "mL", 0.5, T0, tv.astype(np.float32)),
    })  # fmt: skip
    r = disturbance_summary(store, DEV, T0, T0 + timedelta(hours=1))
    rises = [f for f in r.findings if f["kind"] == "pulse_rise"]
    assert [x["linked_to"] for x in rises] == [["recovery_breath"], ["unexplained"]]
    assert rises[0]["breathing_response"]["recovery_ratio"] == pytest.approx(1.75)
    assert r.measured["pulse_rise_breathing_response_pct"].value == 50
    assert r.inferred["unexplained_arousal_index"].value == pytest.approx(1.0)
    assert "calm_breathing_pct" not in r.measured and any("25 Hz" in x for x in r.notes)

# ---- real data ---------------------------------------------------------------------------------

def test_real_coverage_matches_flow_to_pld_ratio(store: Store) -> None:
    for day, expected in ((3, 57.5), (4, 20.2), (5, 18.4), (6, 8.8), (8, 100.0)):
        r = night_disturbances(store, DEV, date(2026, 11, day), CTX)
        lo, hi = day_window(date(2026, 11, day), CTX)
        # independent: 25 Hz sample count / 0.5 Hz PLD sample count, both converted to seconds
        f = sum(len(p.values) for p in store.channel_window(DEV, "flow", lo, hi)) / 25
        pld = sum(len(p.values) for p in store.channel_window(DEV, "flow_limitation", lo, hi)) * 2
        assert r is not None
        assert r.measured["breathing_coverage_pct"].value == pytest.approx(100 * f / pld)
        assert r.measured["breathing_coverage_pct"].value == pytest.approx(expected, abs=0.1)
        assert r.measured["calm_breathing_pct"].value is not None
        assert (expected < 90) == any("covers only" in x for x in r.notes)

def test_real_irregularity_correlates_with_pulse_variability(store: Store) -> None:
    """Restless breathing is physiological, not noise: on the oximetry nights the epoch
    irregularity correlates positively with pulse variability (independent loop for pulse SD)."""
    for day in (date(2026, 10, 19), date(2026, 11, 8)):
        lo, hi = day_window(day, CTX)
        fl = max(store.channel_window(DEV, "flow", lo, hi), key=lambda p: len(p.values))
        pu = next(q for q in store.channel_window(DEV, "pulse", lo, hi) if q.start == fl.start)
        irr = epoch_irregularity(segment_breaths(fl), len(fl.values) / 25)
        xs, ys = [], []
        for e in range(2, len(irr)):
            vals = [float(x) for x in pu.values[e * 30 - 45 : e * 30 + 75] if x == x]
            if len(vals) < 100 or not np.isfinite(irr[e]):
                continue
            mean = sum(vals) / len(vals)
            xs.append(irr[e])
            ys.append((sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5)
        assert np.corrcoef(xs, ys)[0, 1] > 0.3

def test_real_restless_periods_and_reduced_unexplained(store: Store) -> None:
    lo, hi = day_window(date(2026, 11, 8), CTX)
    r = disturbance_summary(store, DEV, lo, hi)
    restless = [f for f in r.findings if f["kind"] == "restless_breathing"]
    assert restless and all(f["duration_s"] >= 60 and f["basis"] == "measured" for f in restless)
    assert all(f["linked_to"] for f in restless)
    # breathing responses explain a large share of the pulse rises (fixture research: ~40-70 %)
    assert 30 < r.measured["pulse_rise_breathing_response_pct"].value < 90
    assert r.inferred["unexplained_arousal_index"].value < 0.7 * r.inferred["autonomic_arousal_index"].value
    strict = disturbance_summary(store, DEV, lo, hi, {"restless_irregularity": 0.5})
    assert strict.measured["calm_breathing_pct"].value > r.measured["calm_breathing_pct"].value
