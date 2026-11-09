"""Per-night views: ``list_nights`` (one row per therapy day) and ``night_summary`` (detail).

Device values (STR) and derived values (from sessions/events/channels) are returned side by
side and never merged; ``notes`` say what is missing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pap_analytics.analytics.events import NOT_RESPIRATORY
from pap_analytics.analytics.results import NightRow, NightSummary, StatBlock
from pap_analytics.analytics.segments import STAT_CHANNELS
from pap_analytics.analytics.stats import has_finite, percentile, seconds_above, stat_block
from pap_analytics.analytics.timeparse import Context, day_window
from pap_analytics.model import CHANNELS, Event, EventType, Metric, Provenance, TherapyDay
from pap_analytics.store import SessionInfo, Store

AHI_TYPES = {EventType.OBSTRUCTIVE_APNEA, EventType.CENTRAL_APNEA, EventType.APNEA, EventType.HYPOPNEA}
FAR_PAST, FAR_FUTURE = date(1900, 1, 1), date(9999, 12, 31)


def _derived(value: float | None, unit: str, method: str) -> Metric:
    return Metric(value, unit, Provenance.DERIVED, method)


def _count(events: list[Event]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        if e.type not in NOT_RESPIRATORY:
            out[e.type.value] = out.get(e.type.value, 0) + 1
    return dict(sorted(out.items()))


def _session_minutes(sessions: list[SessionInfo]) -> float:
    return sum((s.end - s.start).total_seconds() for s in sessions) / 60


def night_mode(sessions: list[SessionInfo]) -> tuple[str | None, dict[str, float]]:
    """Mode the night ran in (the one with the longest total session time) and minutes per mode."""
    minutes: dict[str, float] = {}
    for s in sessions:
        if s.mode:
            minutes[s.mode] = minutes.get(s.mode, 0.0) + (s.end - s.start).total_seconds() / 60
    if not minutes:
        return None, {}
    return max(minutes, key=lambda m: minutes[m]), dict(sorted(minutes.items()))


def settings_in_effect(
    store: Store, device_id: str, day: date, mode: str | None
) -> tuple[dict[str, Any] | None, list[str]]:
    """Latest settings change point on or before ``day``, plus notes. Settings whose mode differs from
    the mode recorded in the night's sessions are returned with ``applies=False``: the device was
    switched between the settings snapshot and this night, so their values do not describe it."""
    history = [s for s in store.settings(device_id) if s.valid_from <= day]
    if not history:
        return None, ["no settings known for this day"]
    s = history[-1]
    out: dict[str, Any] = {"valid_from": s.valid_from, "mode": s.mode, "values": s.values}
    if mode is None or s.mode is None or s.mode == mode:
        return out, []
    out["applies"] = False
    return out, [
        f"the sessions of this night ran in mode {mode!r}, but the latest known settings "
        f"(from {s.valid_from.isoformat()}) are for mode {s.mode!r}; their values do not apply to this night"
    ]


def _ahi(events: list[Event], usage_min: float | None) -> Metric:
    n = sum(1 for e in events if e.type in AHI_TYPES)
    value = n / (usage_min / 60) if usage_min else None
    return _derived(value, "1/h", "apnea+hypopnea events / session hours")


def _day_data(
    store: Store, device_id: str, day: date, ctx: Context, sessions: list[SessionInfo]
) -> tuple[list[Event], datetime, datetime]:
    lo, hi = day_window(day, ctx)
    events: list[Event] = []
    if sessions:
        lo = min(s.start for s in sessions)
        hi = max(s.end for s in sessions) + timedelta(seconds=1)
        events = [e for e in store.events(device_id, lo, hi) if lo <= e.start < hi]
    return events, lo, hi


def _has_oximetry(store: Store, device_id: str, sessions: list[SessionInfo]) -> bool:
    for s in sessions:
        if "spo2" in s.channels and has_finite(store.channel_window(device_id, "spo2", s.start, s.end)):
            return True
    return False


def _row(
    store: Store, device_id: str, day: date, td: TherapyDay | None, sessions: list[SessionInfo], ctx: Context
) -> NightRow:
    events, lo, hi = _day_data(store, device_id, day, ctx, sessions)
    dev_usage = td.usage_s / 60 if td and td.usage_s is not None else None
    der_usage = _session_minutes(sessions) if sessions else None
    usage = (
        Metric(dev_usage, "min", Provenance.DEVICE, "STR.edf daily summary")
        if dev_usage is not None
        else _derived(der_usage, "min", "sum of session durations")
    )
    ahi_dev = td.metrics.get("ahi") if td else None
    ahi_der = _ahi(events, der_usage if der_usage else dev_usage)
    leak_dev = td.metrics.get("leak_p95") if td else None
    leak = leak_dev
    if leak is None or leak.value is None:
        leak = None
        if sessions:
            parts = store.channel_window(device_id, "leak", lo, hi)
            v = percentile(parts, 95)
            leak = _derived(v, "L/s", "p95 of 2 s leak channel") if v is not None else None
    pressure = None
    if sessions:
        v = percentile(store.channel_window(device_id, "pressure", lo, hi), 95)
        pressure = _derived(v, "cmH2O", "p95 of 2 s pressure channel") if v is not None else None
    derived_usage = _derived(der_usage, "min", "sum of session durations")
    return NightRow(
        day,
        usage,
        len(sessions),
        ahi_dev,
        ahi_der,
        _count(events),
        leak,
        pressure,
        _has_oximetry(store, device_id, sessions),
        derived_usage if dev_usage is not None and der_usage else None,
        night_mode(sessions)[0],
    )


def list_nights(
    store: Store, device_id: str, day_from: date, day_to: date, ctx: Context | None = None
) -> list[NightRow]:
    """One row for each therapy day that has sessions or a device summary, in ``[day_from, day_to]``."""
    ctx = ctx or Context()
    tds = {d.date: d for d in store.therapy_days(device_id, day_from, day_to)}
    by_day: dict[date, list[SessionInfo]] = {}
    for s in store.sessions(device_id, day_from, day_to):
        by_day.setdefault(s.therapy_day, []).append(s)
    return [
        _row(store, device_id, d, tds.get(d), by_day.get(d, []), ctx) for d in sorted(set(tds) | set(by_day))
    ]


def _mask_off_gaps(sessions: list[SessionInfo]) -> list[dict[str, Any]]:
    gaps = []
    for a, b in zip(sessions, sessions[1:], strict=False):
        if b.start > a.end:
            gaps.append({"start": a.end, "end": b.start, "minutes": (b.start - a.end).total_seconds() / 60})
    return gaps


def _events_per_hour(events: list[Event], sessions: list[SessionInfo], ctx: Context) -> list[dict[str, Any]]:
    """Local clock-hour buckets: therapy minutes in the hour and respiratory events by AHI type."""
    if not sessions:
        return []
    first = min(s.start for s in sessions).astimezone(ctx.tz).replace(minute=0, second=0, microsecond=0)
    last = max(s.end for s in sessions).astimezone(ctx.tz)
    rows = []
    h = first
    while h < last:
        h_end = h + timedelta(hours=1)
        minutes = sum(
            max(0.0, (min(s.end, h_end) - max(s.start, h)).total_seconds()) for s in sessions
        ) / 60  # fmt: skip
        n = sum(1 for e in events if e.type in AHI_TYPES and h <= e.start < h_end)
        rows.append({"hour_start": h, "usage_min": minutes, "events": n,
                     "per_hour": (n / (minutes / 60)) if minutes >= 1 else None})  # fmt: skip
        h = h_end
    return rows


def night_summary(store: Store, device_id: str, day: date, ctx: Context | None = None) -> NightSummary:
    ctx = ctx or Context()
    sessions = store.sessions(device_id, day, day)
    td = next(iter(store.therapy_days(device_id, day, day)), None)
    events, lo, hi = _day_data(store, device_id, day, ctx, sessions)
    notes: list[str] = []
    device_metrics: dict[str, Metric] = dict(td.metrics) if td else {}
    if td is None:
        notes.append("no device summary (STR) for this day; device metrics are missing")
    if not sessions:
        notes.append("no detailed sessions for this day; only the device summary is available")
    usage_min = _session_minutes(sessions) if sessions else None
    derived: dict[str, Metric] = {}
    if usage_min is not None:
        derived["usage_min"] = _derived(usage_min, "min", "sum of session durations")
        derived["ahi"] = _ahi(events, usage_min)
        for key, types in (("hi", {EventType.HYPOPNEA}),
                           ("ai", AHI_TYPES - {EventType.HYPOPNEA})):  # fmt: skip
            n = sum(1 for e in events if e.type in types)
            derived[key] = _derived(
                n / (usage_min / 60) if usage_min else None, "1/h", f"{key} events / hours"
            )
    if td and td.usage_s is not None and usage_min is not None:
        diff = abs(td.usage_s / 60 - usage_min)
        if diff > 2:
            notes.append(f"device usage and session durations differ by {diff:.0f} min")
    channels: dict[str, StatBlock] = {}
    if sessions:
        for name in STAT_CHANNELS:
            parts = store.channel_window(device_id, name, lo, hi)
            block = stat_block(parts, CHANNELS[name])
            if block:
                channels[name] = block
        leak = store.channel_window(device_id, "leak", lo, hi)
        if leak:
            derived["large_leak_min"] = _derived(
                seconds_above(leak, ctx.large_leak_lps) / 60, "min", f"leak > {ctx.large_leak_lps} L/s"
            )
    mode, mode_minutes = night_mode(sessions)
    if len(mode_minutes) > 1:
        notes.append("sessions ran in different modes: "
                     + ", ".join(f"{m} {v:.0f} min" for m, v in mode_minutes.items()))  # fmt: skip
    settings, settings_notes = settings_in_effect(store, device_id, day, mode)
    notes += settings_notes
    oximetry = None
    if sessions:
        from pap_analytics.analytics import oximetry as ox  # local import: oximetry builds on this module

        oximetry = ox.night_oximetry(store, device_id, day, ctx)
        if oximetry is None:
            notes.append("no valid SpO2 data for this day")
    session_rows = [
        {"id": s.id, "start": s.start, "end": s.end, "minutes": (s.end - s.start).total_seconds() / 60,
         "complete": s.complete, "mode": s.mode, "channels": list(s.channels)} for s in sessions
    ]  # fmt: skip
    return NightSummary(
        day, device_id, session_rows, _mask_off_gaps(sessions), _count(events),
        _events_per_hour(events, sessions, ctx), device_metrics, derived, channels, settings, oximetry, notes,
        mode,
    )  # fmt: skip
