from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from pap_analytics.analytics import Context
from pap_analytics.analytics.disturbances import (
    clean_pulse,
    detect_pulse_rises,
    disturbance_summary,
    disturbance_trend,
    flow_limitation_episodes,
    night_disturbances,
    pulse_response,
)
from pap_analytics.analytics.oximetry import oximetry_summary
from pap_analytics.analytics.timeparse import day_window
from pap_analytics.model import ChannelData, Event, EventType, Provenance
from pap_analytics.store import Store

DEV = "resmed-23262018278"
CTX = Context()
T0 = datetime(2026, 1, 1, tzinfo=UTC)

def ch(name: str, values, rate: float = 1.0) -> ChannelData:
    return ChannelData(name, "", rate, T0, np.asarray(values, dtype=np.float32))

def ev(at_s: float, kind: EventType = EventType.HYPOPNEA, dur: float = 0.0) -> Event:
    return Event(T0 + timedelta(seconds=at_s), dur, kind, kind.value, Provenance.DEVICE)

class FakeStore:
    def __init__(self, channels: dict[str, ChannelData], events=()):
        self.channels, self._events = channels, list(events)

    def channel_window(self, device_id, name, start, end):
        return [self.channels[name]] if name in self.channels else []

    def events(self, device_id, start, end, types=None):
        return list(self._events)

# ---- pulse ---------------------------------------------------------------------------------

def test_pulse_rises_rapid_only() -> None:
    v = np.full(1200, 60.0)
    v[200:206] = 72.0  # rapid +12 for 6 s -> counted
    v[400:402] = 75.0  # +15 for 2 s only -> too short
    v[600:700] = np.linspace(60, 75, 100)  # slow drift over 100 s -> not rapid
    v[700:760] = 75.0
    v[900:910] = 66.0  # +6 < 8 -> too small
    rises = detect_pulse_rises(ch("pulse", v), rise_bpm=8, min_s=3, baseline_s=60)
    assert len(rises) == 1
    r = rises[0]
    assert r["start"] == T0 + timedelta(seconds=200) and r["duration_s"] == 6
    assert r["baseline"] == 60 and r["peak"] == 72 and r["rise"] == 12

def test_clean_pulse_drops_steps_range_and_invalid_spo2() -> None:
    v = np.full(100, 60.0)
    v[10] = 90.0  # +30 step and back: both jumps > 10 /min
    v[20] = 250.0  # out of range
    ok = np.ones(100, dtype=bool)
    ok[30:40] = False
    c = clean_pulse(ch("pulse", v), ok)
    assert np.isnan(c[10]) and np.isnan(c[20]) and np.isnan(c[30:40]).all()
    assert np.isfinite(c[41:100]).all()

def test_prr_exact_and_hypopnea_onset() -> None:
    v = np.full(300, 60.0)
    v[60:90] = 58.0  # pre-event baseline window of the apnea (event at 90 s, 10 s long)
    v[100:130] = np.arange(30) / 2 + 60  # after event end (100 s): peak 74.5 at 129 s
    p = ch("pulse", v)
    r = pulse_response(p, v.astype(np.float64), ev(90, EventType.APNEA, 10))
    assert r is not None and r["baseline"] == 58 and r["peak"] == 74.5 and r["response"] == 16.5
    # hypopnea mark at 100 s: onset assumed 10 s earlier -> same baseline window, same response window
    r2 = pulse_response(p, v.astype(np.float64), ev(100))
    assert r2 is not None and r2["response"] == 16.5
    # no pulse before the event -> not evaluable
    assert pulse_response(p, np.full(300, np.nan), ev(100)) is None

# ---- flow limitation / RERA candidates -------------------------------------------------------

def test_flow_limitation_episodes_bridge_single_gap() -> None:
    fl = np.zeros(100)
    fl[10:14] = 0.3
    fl[15:20] = 0.3  # one-sample dip at 14 is bridged -> 10 samples = 20 s at 0.5 Hz
    fl[50:54] = 0.5  # 8 s -> too short
    fl[70:76] = 0.1  # below threshold
    assert flow_limitation_episodes(ch("flow_limitation", fl, 0.5), 0.2, 10) == [(10, 20)]

def _fl_store(events=()):
    fl = np.zeros(900)  # 0.5 Hz -> 30 min
    tv = np.full(900, 500.0)
    fl[100:110] = 0.4  # 20 s episode
    tv[100:110] = 300.0
    tv[111] = 600.0  # recovery breath: 600 / 300 = 2.0
    fl[400:410] = 0.4  # episode without recovery
    tv[400:410] = 400.0  # back to 500 afterwards: 500 / 400 = 1.25 < 1.5 -> no recovery breath
    return FakeStore(
        {"flow_limitation": ch("flow_limitation", fl, 0.5), "tidal_volume": ch("tidal_volume", tv, 0.5)},
        events,
    )

def test_rera_candidates_and_flow_metrics() -> None:
    r = disturbance_summary(_fl_store(), DEV, T0, T0 + timedelta(minutes=30))
    assert r.flow_limitation_s == 1800
    m, i = r.measured, r.inferred
    # 40 s of 1800 s flow-limited; 2 episodes in 0.5 h
    assert m["flow_limitation_time_pct"].value == pytest.approx(100 * 40 / 1800)
    assert m["flow_limitation_index"].value == pytest.approx(4.0)
    assert i["rera_candidate_index"].value == pytest.approx(2.0)  # 1 candidate / 0.5 h
    rera = [f for f in r.findings if f["kind"] == "rera_candidate"]
    assert len(rera) == 1 and rera[0]["recovery_ratio"] == 2.0 and rera[0]["basis"] == "inferred"
    assert rera[0]["start"] == T0 + timedelta(seconds=200)
    assert all(x.provenance is Provenance.DERIVED for x in (*m.values(), *i.values()))
    assert "hypoxic_burden" not in m and any("pulse" in n for n in r.notes)

def test_rera_candidate_excluded_when_device_event_overlaps() -> None:
    r = disturbance_summary(_fl_store([ev(215)]), DEV, T0, T0 + timedelta(minutes=30))
    assert r.inferred["rera_candidate_index"].value == 0
    assert r.inferred["rdi_inferred"].value == pytest.approx(2.0)  # 1 device hypopnea / 0.5 h

# ---- hypoxic burden / linking -----------------------------------------------------------------

def test_hypoxic_burden_manual_area() -> None:
    # base 97, 3 s ramp down to 93, 13 s at 93, 3 s ramp back up (values below are exact samples)
    v = np.full(3600, 97.0)
    v[300:303] = [95.0, 94.0, 93.0]
    v[303:316] = 93.0
    v[316:319] = [94.0, 95.0, 96.5]
    # core (<= 94): 301..316 -> the >= 3 % desaturation lasts 16 s (>= 10 s).
    # Area below baseline 97: extends back to 300 (95 < 97) and forward to 317 (95 < 96); 318 (96.5) is
    # within 1 % of baseline. Samples 300..317: 2 + 3 + 4*14 + 3 + 2 = 66 %.s -> 1.1 %.min per valid hour.
    r = oximetry_summary(FakeStore({"spo2": ch("spo2", v)}), DEV, T0, T0 + timedelta(hours=1))
    assert r is not None and r.metrics["desat3_count"].value == 1
    assert r.metrics["hypoxic_burden"].value == pytest.approx(66 / 60)
    assert r.desaturations[0]["area_pct_min"] == pytest.approx(66 / 60)

def test_pulse_rise_linking() -> None:
    spo2 = np.full(3600, 97.0)
    pulse = np.full(3600, 60.0)
    # ramps of 5 /min per second (larger 1 s steps are filtered as artefacts)
    for at in (998, 1998):
        pulse[at : at + 12] = [65, 70, 75, 75, 75, 75, 75, 75, 75, 75, 70, 65]
    # rise 1 reaches baseline 60 + 8 at 999 s: 4 s after the apnea end (995 s) -> linked;
    # rise 2 at 1999 s has nothing nearby -> unexplained
    store = FakeStore(
        {"spo2": ch("spo2", spo2), "pulse": ch("pulse", pulse)}, [ev(985, EventType.APNEA, 10)]
    )
    r = disturbance_summary(store, DEV, T0, T0 + timedelta(hours=1))
    rises = [f for f in r.findings if f["kind"] == "pulse_rise"]
    assert [x["linked_to"] for x in rises] == [["respiratory_event"], ["unexplained"]]
    assert r.measured["pulse_rise_index"].value == pytest.approx(2.0)
    assert r.inferred["autonomic_arousal_index"].value == pytest.approx(2.0)
    assert r.inferred["respiratory_arousal_index"].value == pytest.approx(1.0)
    assert r.inferred["unexplained_arousal_index"].value == pytest.approx(1.0)
    # PRR of the apnea: baseline 60, peak 75 within 30 s after the end at 995 s
    assert r.measured["prr_median"].value == 15 and r.measured["prr_responsive_pct"].value == 100
    assert r.event_responses[0]["event_type"] == "apnea"

# ---- real data ---------------------------------------------------------------------------------

def _loop_prr(pulse: list[float], start: datetime, event: Event) -> float | None:
    """Independent naive PRR (no shared code): 1 Hz pulse list starting at ``start``."""
    onset = event.start if event.duration_s > 0 else event.start - timedelta(seconds=10)
    end = event.start + timedelta(seconds=event.duration_s)
    i_on = int(-(-(onset - start).total_seconds() // 1))
    i_end = int(-(-(end - start).total_seconds() // 1))
    if i_on - 30 < 0 or i_end + 30 > len(pulse):
        return None
    pre = sorted(x for x in pulse[i_on - 30 : i_on] if x == x)
    post = [x for x in pulse[i_end : i_end + 30] if x == x]
    if len(pre) < 15 or len(post) < 15:
        return None
    n = len(pre)
    med = pre[n // 2] if n % 2 else (pre[n // 2 - 1] + pre[n // 2]) / 2
    return max(post) - med

def test_real_2026_11_08_prr_independent(store: Store) -> None:
    lo, hi = day_window(date(2026, 11, 8), CTX)
    r = disturbance_summary(store, DEV, lo, hi)
    resp = [e for e in store.events(DEV, lo, hi) if e.type.value in ("hypopnea", "apnea") and e.start >= lo]
    assert len(resp) == 7  # 6 hypopneas + 1 apnea (tests/data/README: device EVE)
    spo2 = {p.start: p for p in store.channel_window(DEV, "spo2", lo, hi)}
    expected = []
    from pap_analytics.analytics.oximetry import filter_artefacts

    for q in store.channel_window(DEV, "pulse", lo, hi):
        ok = np.isfinite(filter_artefacts(spo2[q.start]))
        vals = [float(x) if ok[k] and 30 <= x <= 220 else float("nan") for k, x in enumerate(q.values)]
        # independent step filter: drop a sample that jumps > 10 from its (masked) predecessor
        for k in range(len(vals) - 1, 0, -1):
            if vals[k] == vals[k] and vals[k - 1] == vals[k - 1] and abs(vals[k] - vals[k - 1]) > 10:
                vals[k] = float("nan")
        expected += [x for e in resp if (x := _loop_prr(vals, q.start, e)) is not None]
    got = sorted(x["response"] for x in r.event_responses)
    assert got == pytest.approx(sorted(expected)) and len(got) >= 5
    assert r.measured["prr_median"].value == pytest.approx(float(np.median(expected)))
    # plausibility: pulse rises happen, but well below one per minute; hypoxic burden small
    assert 1 < r.measured["pulse_rise_index"].value < 30
    assert 0 < r.measured["hypoxic_burden"].value < 20
    assert r.pulse_valid_s == pytest.approx(r.flow_limitation_s, rel=0.05)
    kinds = {f["basis"] for f in r.findings}
    assert kinds <= {"measured", "inferred"}

def test_real_night_without_oximetry_keeps_flow_metrics(store: Store) -> None:
    r = night_disturbances(store, DEV, date(2026, 11, 6), CTX)
    assert r is not None and r.pulse_valid_s == 0
    assert "prr_median" not in r.measured and "autonomic_arousal_index" not in r.inferred
    assert any("no valid pulse" in n for n in r.notes)
    # independent loop over the raw device channel: runs >= 0.2 lasting >= 5 samples (10 s),
    # single sub-threshold samples between two flow-limited samples bridged
    lo, hi = day_window(date(2026, 11, 6), CTX)
    count = 0
    for p in store.channel_window(DEV, "flow_limitation", lo, hi):
        v = [float(x) if x == x else 0.0 for x in p.values]
        on = [x >= 0.2 for x in v]
        for k in range(1, len(v) - 1):
            if not on[k] and v[k - 1] >= 0.2 and v[k + 1] >= 0.2:
                on[k] = True
        run = 0
        for flag in [*on, False]:
            if flag:
                run += 1
            else:
                count += run >= 5
                run = 0
    hours = r.flow_limitation_s / 3600
    assert count > 0
    assert r.measured["flow_limitation_index"].value == pytest.approx(count / hours)

def test_trend_rows_and_nulls(store: Store) -> None:
    t = disturbance_trend(store, DEV, date(2026, 11, 3), date(2026, 11, 8), CTX)
    days = [r["date"] for r in t["nights"]]
    assert days == [date(2026, 11, d) for d in (3, 4, 5, 6, 7, 8)]
    by_day = {r["date"]: r for r in t["nights"]}
    assert by_day[date(2026, 11, 4)]["prr_median"] is None  # no valid oximetry that night
    assert by_day[date(2026, 11, 8)]["pulse_rise_index"] is not None
    assert all(r["flow_limitation_index"] is not None for r in t["nights"])
    info = t["metrics"]["pulse_rise_index"]
    assert info["basis"] == "measured" and info["nights"] == 2 and info["unit"] == "1/h"
    assert t["metrics"]["rera_candidate_index"]["basis"] == "inferred"
    assert info["mean"] == pytest.approx(
        np.mean([by_day[date(2026, 11, d)]["pulse_rise_index"] for d in (3, 8)])
    )

def test_thresholds_override_changes_detection(store: Store) -> None:
    lo, hi = day_window(date(2026, 11, 8), CTX)
    default = disturbance_summary(store, DEV, lo, hi)
    strict = disturbance_summary(store, DEV, lo, hi, {"pulse_rise_bpm": 15})
    assert strict.measured["pulse_rise_index"].value < default.measured["pulse_rise_index"].value
