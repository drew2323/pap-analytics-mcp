"""Event listing and clustering."""

from __future__ import annotations

from datetime import datetime, timedelta

from pap_analytics.analytics.results import EventCluster, EventsResult
from pap_analytics.analytics.timeparse import Context
from pap_analytics.model import Event, EventType
from pap_analytics.store import Store

NOT_RESPIRATORY = {EventType.RECORDING_START}


def event_dict(e: Event) -> dict[str, object]:
    return {
        "start": e.start, "duration_s": e.duration_s, "type": e.type.value,
        "raw_type": e.raw_type, "provenance": e.provenance.value,
    }  # fmt: skip


def get_events(
    store: Store, device_id: str, start: datetime, end: datetime,
    types: set[EventType] | None = None, limit: int = 200, ctx: Context | None = None,
) -> EventsResult:  # fmt: skip
    """Events starting in ``[start, end)``, chronological, at most ``limit``."""
    evs = [e for e in store.events(device_id, start, end, types) if start <= e.start < end]
    notes = []
    if len(evs) > limit:
        notes.append(
            f"truncated to the first {limit} of {len(evs)} events; narrow the window or filter types"
        )
    shown = evs[:limit]
    return EventsResult(start, end, len(evs), len(shown), [event_dict(e) for e in shown], notes)


def event_clusters(events: list[Event], min_events: int = 3, window_min: float = 10) -> list[EventCluster]:
    """Group respiratory events where each begins within ``window_min`` of the previous one's end;
    keep groups with at least ``min_events`` events."""
    evs = sorted((e for e in events if e.type not in NOT_RESPIRATORY), key=lambda e: e.start)
    gap = timedelta(minutes=window_min)
    out: list[EventCluster] = []
    group: list[Event] = []
    group_end: datetime | None = None

    def flush() -> None:
        if len(group) >= min_events:
            by_type: dict[str, int] = {}
            for g in group:
                by_type[g.type.value] = by_type.get(g.type.value, 0) + 1
            assert group_end is not None
            out.append(EventCluster(group[0].start, group_end, len(group), by_type))

    for e in evs:
        e_end = e.start + timedelta(seconds=e.duration_s)
        if group and group_end is not None and e.start - group_end <= gap:
            group.append(e)
            group_end = max(group_end, e_end)
        else:
            flush()
            group, group_end = [e], e_end
    flush()
    return out
