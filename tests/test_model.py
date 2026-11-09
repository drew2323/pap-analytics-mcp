from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import numpy as np

from pap_analytics.model import CHANNELS, SCHEMA_VERSION, ChannelData, EventType, session_id, therapy_day

PRAGUE = ZoneInfo("Europe/Prague")


def test_therapy_day_boundaries() -> None:
    assert therapy_day(datetime(2026, 11, 8, 20, 30, tzinfo=PRAGUE), PRAGUE) == date(2026, 11, 8)
    assert therapy_day(datetime(2026, 11, 9, 6, 0, tzinfo=PRAGUE), PRAGUE) == date(2026, 11, 8)
    assert therapy_day(datetime(2026, 11, 9, 12, 0, tzinfo=PRAGUE), PRAGUE) == date(2026, 11, 9)
    assert therapy_day(datetime(2026, 11, 9, 11, 59, tzinfo=PRAGUE), PRAGUE) == date(2026, 11, 8)


def test_therapy_day_dst_end_both_offsets() -> None:
    # 2026-10-25 02:30 happens twice in Prague (CEST +02:00, then CET +01:00).
    first = datetime(2026, 10, 25, 2, 30, tzinfo=PRAGUE, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=PRAGUE, fold=1)
    assert first.utcoffset() != second.utcoffset()
    assert therapy_day(first, PRAGUE) == date(2026, 10, 24)
    assert therapy_day(second, PRAGUE) == date(2026, 10, 24)


def test_therapy_day_from_utc_and_custom_boundary() -> None:
    ts = datetime(2026, 11, 9, 4, 0, tzinfo=UTC)  # 05:00 Prague
    assert therapy_day(ts, PRAGUE) == date(2026, 11, 8)
    assert therapy_day(ts, PRAGUE, boundary_hour=4) == date(2026, 11, 9)


def test_session_id_uses_utc() -> None:
    start = datetime(2026, 11, 8, 23, 5, 7, tzinfo=PRAGUE)
    assert session_id("resmed:123", start) == "resmed:123:20261108T220507"


def test_constants_and_channeldata() -> None:
    assert SCHEMA_VERSION == 1
    assert CHANNELS["flow"] == "L/s" and CHANNELS["spo2"] == "%"
    assert EventType("hypopnea") is EventType.HYPOPNEA
    ch = ChannelData("flow", "L/s", 25.0, datetime(2026, 1, 1, tzinfo=UTC), np.zeros(3, dtype=np.float32))
    assert ch.values.dtype == np.float32
