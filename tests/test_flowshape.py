from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from pap_analytics.analytics import Context, compare_periods
from pap_analytics.analytics.breathing import segment_breaths
from pap_analytics.analytics.disturbances import disturbance_trend, night_disturbances
from pap_analytics.analytics.flowshape import breath_shapes, flow_shape, rera_runs
from pap_analytics.analytics.timeparse import day_window
from pap_analytics.model import ChannelData
from pap_analytics.store import Store

DEV = "resmed-23262018278"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
RATE = 25.0
TI, TE = 1.6, 2.4  # s; 40 + 60 samples

def ch(name: str, values, rate: float = RATE, start: datetime = T0) -> ChannelData:
    return ChannelData(name, "", rate, start, np.asarray(values, dtype=np.float32))

def breath(insp: np.ndarray, exp_amp: float = 0.5) -> np.ndarray:
    """One breath: the given inspiration (L/s, 40 samples) + a half-sine expiration of ``exp_amp``."""
    te = np.arange(int(TE * RATE)) / RATE
    return np.concatenate((insp, -exp_amp * np.sin(np.pi * te / TE)))

def sine_insp(amp: float = 0.5) -> np.ndarray:
    t = (np.arange(int(TI * RATE)) + 0.5) / RATE
    return amp * np.sin(np.pi * t / TI)

def ned_insp(amp: float = 0.5) -> np.ndarray:
    """Peak ``amp`` early, then a 0.6 x amp plateau over mid-inspiration: NED = 40 % by construction."""
    n = int(TI * RATE)
    x = np.interp(np.arange(n), [0, 8, 12, 32, n - 1], [0.05, amp, 0.6 * amp, 0.6 * amp, 0.05])
    return x

def shapes_of(values):
    f = ch("flow", values)
    b = segment_breaths(f)
    return b, breath_shapes(f, b)

def test_sine_breath_ned_flatness_and_glasgow_flags() -> None:
    b, s = shapes_of(np.concatenate([breath(sine_insp())] * 30))
    v = s.valid
    assert v.sum() >= 25
    # half sine: flow at mid-inspiration = peak -> NED ~ 0; mean / peak = 2 / pi
    assert np.nanmax(s.ned_pct[v]) < 2
    assert np.nanmedian(s.fi[v]) == pytest.approx(2 / np.pi, abs=0.02)
    assert np.nanmedian(s.qpeak_lpm[v]) == pytest.approx(30.0, rel=0.01)  # 0.5 L/s
    # sine above the +5 L/min grey zone (sin > 1/6, 89.3 % of Ti) with 28.7 % of Ti above 90 % of the
    # peak (1 - 2 asin(0.9) / pi): 28.7 / 89.3 = 32 % -> neither spike (< 20 %) nor top heavy (> 40 %)
    for k in ("skew", "spike", "top_heavy", "multi_peak", "multi_breath", "variable_amp"):
        assert not s.glasgow[k][v].any(), k
    # 4 s breaths = 15 /min, not above 20 /min
    assert not s.glasgow["inspir_rate"][v].any()

def test_spike_variable_amplitude_and_multi_breath_flags() -> None:
    # triangle 0.15 -> 0.5 -> 0.15 L/s over samples 1..38 (all above the +5 L/min grey zone): samples
    # above 90 % of the peak (0.45 L/s) are |i - 20| < 19 x 0.05 / 0.35 = 2.7 -> 5 of 38 = 13 % < 20 %
    spike = np.interp(np.arange(40), [0, 1, 20, 38, 39], [0.05, 0.15, 0.5, 0.15, 0.05])
    _, s = shapes_of(np.concatenate([breath(spike)] * 20))
    assert s.glasgow["spike"][s.valid].all()
    # alternating peaks 24 / 36 L/min: variance of 5 peaks >= 28.8 (L/min)^2 > 4 -> variable amplitude
    amps = [0.4, 0.6] * 15
    _, s = shapes_of(np.concatenate([breath(sine_insp(a)) for a in amps]))
    flagged = s.glasgow["variable_amp"][s.valid]
    assert flagged[6:].all() and not flagged[:5].any()
    # expiration peak 0.1 L/s = 6 L/min never below -10 L/min -> no exhale between inspirations
    _, s = shapes_of(np.concatenate([breath(sine_insp(), exp_amp=0.1)] * 20))
    assert s.glasgow["multi_breath"][s.valid][1:].all()

def test_ned_of_constructed_flow_limited_breath() -> None:
    _, s = shapes_of(np.concatenate([breath(ned_insp())] * 10))
    ned = s.ned_pct[s.valid]
    assert np.median(ned) == pytest.approx(40.0, abs=1.0)
    assert s.flow_limited(20.0)[s.valid].all() and not s.flow_limited(45.0)[s.valid].any()

def test_ned_rera_needs_a_run_ended_by_a_sigh() -> None:
    normal = [breath(sine_insp(0.5))] * 20
    limited = [breath(ned_insp(0.4))] * 5
    b, s = shapes_of(np.concatenate(normal + limited + [breath(sine_insp(0.8))] + normal))
    runs = rera_runs(s)
    assert len(runs) == 1
    i, j, det = runs[0]
    assert j - i == 5 and det["max_ned_pct"] == pytest.approx(40, abs=1)
    assert det["sigh_ratio"] == pytest.approx(48 / 24, rel=0.02)  # 0.8 vs 0.4 L/s peaks
    assert b.start_s[i] == pytest.approx(20 * (TI + TE), abs=0.1)
    # same run ended by an ordinary breath (peak ratio 0.5 / 0.4 = 1.25 < 1.5): no candidate
    _, s = shapes_of(np.concatenate(normal + limited + normal))
    assert rera_runs(s) == []
    assert len(rera_runs(s, sigh_ratio=1.2)) == 1
    # two flow-limited breaths are not a run
    _, s = shapes_of(np.concatenate(normal + limited[:2] + [breath(sine_insp(0.8))] + normal))
    assert rera_runs(s) == []

def test_flow_shape_split_by_pressure_support() -> None:
    n = 60  # 4 s breaths -> 240 s
    f = ch("flow", np.concatenate([breath(sine_insp())] * n))
    ps = np.where(np.arange(120) < 60, 0.0, 3.0)  # 0.5 Hz: PS 0 for the first 120 s, then 3 cmH2O
    epap = ch("epap", np.full(120, 5.0), 0.5)
    pressure = ch("pressure", 5.0 + ps, 0.5)
    r = flow_shape([f], [pressure], [epap])
    g0, g1 = r.by_ps["ps_0"]["breaths"].value, r.by_ps["ps_gt_0"]["breaths"].value
    assert g0 + g1 == r.breaths and abs(g0 - g1) <= 2
    assert r.metrics["flow_shape_ps_pct"].value == pytest.approx(100 * g1 / r.breaths)
    assert r.metrics["glasgow_index"].provenance.value == "derived"
    assert flow_shape([f], [], []).by_ps == {}  # no pressure context -> no split, metrics still there
    assert "ned_mean_pct" in flow_shape([f], [], []).metrics

# ---- real data ---------------------------------------------------------------------------------

def night_shape(store: Store, d: date):
    lo, hi = day_window(d, Context())
    return flow_shape(
        store.channel_window(DEV, "flow", lo, hi), store.channel_window(DEV, "pressure", lo, hi),
        store.channel_window(DEV, "epap", lo, hi),
    )  # fmt: skip

# Reference values from upstream AirwayLab (lib/analyzers at 350571d, run with node 26 on the
# same 25 Hz flow exported from the store x 60, sessions as in the app): computeNightGlasgow overall
# and computeNED nedMean. Ours differ by the shared breath segmentation (`segment_breaths` drops
# breaths with phases < 0.3 s or cycles > 20 s; upstream counts every inspiration peak).
UPSTREAM = {date(2026, 10, 16): (2.00, 12.18), date(2026, 10, 22): (2.07, 14.83),
            date(2026, 11, 8): (1.79, 8.21)}  # fmt: skip

@pytest.mark.parametrize("d", sorted(UPSTREAM))
def test_real_nights_match_upstream_airwaylab(store: Store, d: date) -> None:
    glasgow, ned = UPSTREAM[d]
    r = night_shape(store, d)
    assert r.breaths > 5000
    assert r.metrics["glasgow_index"].value == pytest.approx(glasgow, abs=0.3)
    assert r.metrics["ned_mean_pct"].value == pytest.approx(ned, rel=0.1)

def test_real_ned_tracks_device_ahi(store: Store) -> None:
    # Device AHI from STR (tests/data/README.md, resmed-air11-str-20261016), independent of the flow.
    ahi = {16: 1.2, 17: 0.4, 19: 2.0, 20: 4.3, 21: 2.9, 22: 4.6, 23: 1.5, 24: 0.4, 25: 0.5}
    days = sorted(ahi)
    ned = [night_shape(store, date(2026, 10, d)).metrics["ned_flow_limited_pct"].value for d in days]

    def ranks(x) -> np.ndarray:
        return np.argsort(np.argsort(np.asarray(x)))

    rho = np.corrcoef(ranks(ned), ranks([ahi[d] for d in days]))[0, 1]
    assert rho > 0.5

def test_real_summary_trend_and_compare(store: Store) -> None:
    d = date(2026, 11, 3)  # CPAP night: no pressure support
    r = night_disturbances(store, DEV, d, Context())
    assert r is not None
    assert r.measured["flow_shape_ps_pct"].value == pytest.approx(0, abs=1)
    assert r.flow_shape_by_ps["ps_gt_0"] == {} and r.flow_shape_by_ps["ps_0"]["breaths"].value > 1000
    rera = [f for f in r.findings if f["kind"] == "rera_candidate"]
    assert rera and all(f["sources"] for f in rera)
    t = disturbance_trend(store, DEV, date(2026, 11, 7), date(2026, 11, 8))
    assert [row["date"] for row in t["nights"]] == [date(2026, 11, 7), date(2026, 11, 8)]
    for key in ("glasgow_index", "ned_flow_limited_pct", "ned_rera_index", "glasgow_variable_amp_pct"):
        assert all(row[key] is not None for row in t["nights"]), key
        assert t["metrics"][key]["provenance"] == "derived"
    assert t["metrics"]["ned_rera_index"]["basis"] == "inferred"
    assert t["metrics"]["glasgow_index"]["basis"] == "measured"
    units = {k: m["unit"] for k, m in t["metrics"].items()}
    c = compare_periods([], [], t["nights"][:1], t["nights"][1:], units)
    g = c.metrics["glasgow_index"]
    assert g["delta"] == pytest.approx(t["nights"][1]["glasgow_index"] - t["nights"][0]["glasgow_index"])

def test_real_threshold_override_changes_ned_share(store: Store) -> None:
    lo, hi = day_window(date(2026, 11, 8), Context())
    flow = store.channel_window(DEV, "flow", lo, hi)
    a = flow_shape(flow, [], [], {"ned_fl_pct": 20}).metrics["ned_flow_limited_pct"].value
    b = flow_shape(flow, [], [], {"ned_fl_pct": 40}).metrics["ned_flow_limited_pct"].value
    assert b < a
    assert lo < hi - timedelta(hours=1)
