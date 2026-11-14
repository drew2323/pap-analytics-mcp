"""MCP tools, resources and prompt: a thin layer over ``analytics`` (never adapters).

Every tool opens the store read-only for the duration of the call, so data written by
`pap ingest` appears without restarting the server. Failures are raised as ``ToolError`` with
text that says what to do next. Further tool modules register through ``add_extension``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from pap_analytics import analytics
from pap_analytics.analytics import AnalyticsError, Context, to_dict
from pap_analytics.analytics import disturbances as dist
from pap_analytics.analytics import oximetry as ox
from pap_analytics.analytics.nights import night_mode, settings_in_effect
from pap_analytics.analytics.timeparse import day_window, resolve_device
from pap_analytics.config import Config
from pap_analytics.model import CHANNEL_ALIASES, CHANNELS, EventType
from pap_analytics.store import SchemaMismatchError, Store, StoreLockedError
from pap_analytics.sync import read_state as read_sync_state

DISCLAIMER = (
    "Analytical output only, not a diagnosis. Discuss clinical questions with the treating physician."
)
FAR_PAST, FAR_FUTURE = date(1900, 1, 1), date(9999, 12, 31)

GLOSSARY = f"""# PAP analytics glossary

**Therapy day**: noon-to-noon local time; a night belongs to the date on which it started
(02:10 on 2026-11-08 means the morning of 2026-11-09). Times in tool arguments are `HH:MM`
within that therapy day, or ISO datetimes.

**Provenance**: `device` = value reported by the device (e.g. ResMed STR AHI, EVE events);
`derived` = computed by this tool (e.g. leak p95 from the 2 s channel, ODI). Both may appear
side by side and can differ slightly; derived values never replace device values.

## Channels (canonical units)
{chr(10).join(f"- `{k}`: {v or 'unitless'}" for k, v in CHANNELS.items())}
`flow` and `pressure_hr` are waveform-rate channels (25 Hz): windows up to 30 min only.
`pressure_hr` is the **25 Hz mask pressure** (alias `mask_pressure_hr`); `mask_pressure` is the same
quantity at only 0.5 Hz (one sample per 2 s) and has no finer detail. For a detailed look at mask
pressure together with flow use `pressure_hr`.
Use `get_signals` to get several channels at their original resolution on one time grid.

## Event types
{chr(10).join(f"- `{e.value}`" for e in EventType)}
`desaturation` is derived only (never from the device).

## Therapy mode
Each session carries the `mode` it actually ran in, read from its recorded data (derived; `cpap`,
`apap`, `asv`, or null when too short to tell). The device can be switched between modes from night
to night, while device settings (`get_settings`) are only known for the days the device summary
covers. A night's `mode` is the session mode with the longest use. When it differs from the mode of
the latest known settings, those settings are marked `applies: false` and must not be used to
describe that night.

## Disturbances beyond AHI (`disturbance_summary`, `disturbance_trend`)
All derived, in two groups that must not be mixed up:
- `measured` – read directly off a signal: `prr_*` (pulse rate response = peak pulse within 30 s
  after a device apnea/hypopnea minus the 30 s pre-event median), `pulse_rise_index` (rapid pulse
  rises >= 8 /min above the 60 s baseline per valid pulse hour), `hypoxic_burden` (%·min/h area of
  >= 3 % desaturations below their baseline; an approximation, not the published event-window HB),
  `flow_limitation_time_pct` / `flow_limitation_index` (device flow-limitation channel >= 0.2),
  breathing stability from the 25 Hz flow: `calm_breathing_pct`, `longest_calm_min`,
  `restless_periods_per_h`, `breathing_irregularity_median` and `breathing_coverage_pct` (share of the
  session with 25 Hz flow; partial on some nights). **Calm/restless describe how regular breathing
  is, not sleep stages** (no EEG). Each pulse rise carries its `breathing_response` (recovery breath
  after, reduced breathing before). Inspiratory flow shape per breath from the 25 Hz flow
  (AirwayLab methods): `ned_mean_pct` / `ned_flow_limited_pct` (negative effort dependence: flow
  drop at mid-inspiration vs the peak), `glasgow_index` (0-9, sum of nine shape components) and the
  shares of breaths flagged `glasgow_skew_pct`, `glasgow_spike_pct`, `glasgow_multi_breath_pct`,
  `glasgow_variable_amp_pct`. Pressure support reshapes inspiration: `flow_shape_ps_pct` is the share
  of breaths with PS and `flow_shape_by_ps` splits the shape metrics into PS = 0 and PS > 0; compare
  nights within the same group, a shape change with a different PS share is not an airway change.
- `inferred` – interpretations, not scored events: possible autonomic arousals (pulse rises,
  split into linked to a respiratory event/desaturation/flow limitation/NED RERA vs `unexplained`),
  RERA candidates (flow limitation from the device channel or from NED ended by a recovery breath,
  outside device AHI events; `sources` says which), `ned_rera_index` and `rdi_inferred`.
  No EEG is recorded: these are never arousals or RERAs in the clinical sense and never change the
  device AHI.
Pulse metrics need oximetry (only some nights); flow metrics exist for every detailed night.

## Notes
- AHI = (apneas + hypopneas) per hour of use. Leak is in L/s (0.4 L/s = 24 L/min).
- `coverage_pct` is the share of the window with valid samples.
- {DISCLAIMER}
"""


def _source_status(config: Config, last: object, newest: date | None) -> list[dict[str, Any]]:
    """Per source: sync/ingest timestamps and newest therapy day, so agents can state data age."""
    sync_state = read_sync_state(config.store_dir)
    ingest_at = getattr(last, "finished", None)
    out = []
    for src in config.sources:
        st = sync_state.get(src.label, {})
        out.append(
            {
                "name": src.label,
                "has_sync": src.sync is not None,
                "last_sync_at": st.get("last_sync_at"),
                "last_sync_ok": st.get("last_sync_ok"),
                "last_sync_error": st.get("error"),
                "last_ingest_at": ingest_at,
                "newest_therapy_day": newest,
            }
        )
    return out


@dataclass
class ServerState:
    config: Config
    ctx: Context
    read_only: bool

    @contextmanager
    def store(self) -> Iterator[Store]:
        try:
            with Store.open(self.config.store_dir, read_only=True) as s:
                yield s
        except FileNotFoundError:
            raise ToolError("No data yet: run `pap ingest` to import a source first.") from None
        except (SchemaMismatchError, StoreLockedError) as exc:
            raise ToolError(str(exc)) from None

    def device(self, store: Store, device_id: str | None) -> str:
        try:
            return resolve_device(store, device_id)
        except AnalyticsError as exc:
            raise ToolError(str(exc)) from None

    def day(self, store: Store, device_id: str, value: str) -> date:
        """Parse a day and make sure it exists; otherwise suggest the nearest available days."""
        try:
            d = analytics.parse_day(value, self.ctx, store, device_id)
        except AnalyticsError as exc:
            raise ToolError(str(exc)) from None
        have = sorted(
            {s.therapy_day for s in store.sessions(device_id)}
            | {t.date for t in store.therapy_days(device_id, FAR_PAST, FAR_FUTURE)}
        )
        if d not in have:
            near = sorted(have, key=lambda x: (abs((x - d).days), x))[:3]
            hint = (
                f"nearest available days: {', '.join(x.isoformat() for x in sorted(near))}"
                if near
                else "store is empty"
            )
            raise ToolError(f"No data for {d.isoformat()}; {hint}.")
        return d

    def window(self, d: date, start: str | None, end: str | None) -> tuple[datetime, datetime]:
        lo, hi = day_window(d, self.ctx)
        try:
            a = analytics.parse_time(start, d, self.ctx) if start else lo
            b = analytics.parse_time(end, d, self.ctx) if end else hi
        except AnalyticsError as exc:
            raise ToolError(str(exc)) from None
        return a, b

    def out(self, obj: Any) -> dict[str, Any]:
        data = to_dict(obj, self.ctx.tz)
        return data if isinstance(data, dict) else {"result": data}


Extension = Callable[[FastMCP, ServerState], None]
_extensions: list[Extension] = []


def add_extension(register: Extension) -> None:
    """Let another module (e.g. anomalies/plots) add tools: ``register(server, state)``."""
    if register not in _extensions:
        _extensions.append(register)


def _guard(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Convert analytics errors to ToolError."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except AnalyticsError as exc:
            raise ToolError(str(exc)) from None

    return wrapper


def build_server(
    config: Config, *, read_only: bool = False, host: str = "127.0.0.1", port: int = 8765
) -> FastMCP:
    state = ServerState(config, Context.from_config(config), read_only)
    server = FastMCP(
        "pap-analytics",
        instructions=f"PAP therapy analytics. Read pap://glossary first. {DISCLAIMER}",
        host=host,
        port=port,
    )
    server.state = state  # type: ignore[attr-defined]
    ctx = state.ctx

    @server.tool()
    @_guard
    def list_devices() -> dict[str, Any]:
        """Known devices with their data range and the time of the last ingest."""
        with state.store() as s:
            rows = []
            for d in s.devices():
                days = [x.therapy_day for x in s.sessions(d.id)] + [
                    t.date for t in s.therapy_days(d.id, FAR_PAST, FAR_FUTURE)
                ]
                rows.append(
                    {
                        **to_dict(d, ctx.tz),
                        "first_day": min(days, default=None),
                        "last_day": max(days, default=None),
                    }
                )
            last = s.last_ingest()
            newest = max((r["last_day"] for r in rows if r["last_day"]), default=None)
            return state.out(
                {
                    "devices": rows,
                    "last_ingest": last.finished if last else None,
                    "sources": _source_status(state.config, last, newest),
                }
            )

    @server.tool()
    @_guard
    def list_nights(
        date_from: str | None = None, date_to: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """One row per night (usage, AHI device+derived, events, leak/pressure p95, oximetry flag).
        Default: the last 14 days of data. Dates: YYYY-MM-DD, `last`, `yesterday`."""
        with state.store() as s:
            dev = state.device(s, device_id)
            try:
                end = analytics.parse_day(date_to or "last", ctx, s, dev)
                start = analytics.parse_day(date_from, ctx, s, dev) if date_from else end - timedelta(days=13)
            except AnalyticsError as exc:
                raise ToolError(str(exc)) from None
            rows = analytics.list_nights(s, dev, start, end, ctx)
            notes = [] if rows else ["no nights in this range"]
            return state.out({"device_id": dev, "nights": rows, "notes": notes})

    @server.tool()
    @_guard
    def night_summary(date: str, device_id: str | None = None) -> dict[str, Any]:
        """Detail of one night: sessions, mask-off gaps, events, device vs derived metrics, channel
        statistics, settings in effect and oximetry. `date` = YYYY-MM-DD or `last`."""
        with state.store() as s:
            dev = state.device(s, device_id)
            return state.out(analytics.night_summary(s, dev, state.day(s, dev, date), ctx))

    @server.tool()
    @_guard
    def get_events(
        date: str, start: str | None = None, end: str | None = None, types: list[str] | None = None,
        limit: int = 200, device_id: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Events in a window of the therapy day (default: whole night). `start`/`end` are HH:MM
        within the therapy day or ISO datetimes; `types` filters by canonical event type."""
        with state.store() as s:
            dev = state.device(s, device_id)
            d = state.day(s, dev, date)
            a, b = state.window(d, start, end)
            wanted = None
            if types:
                valid = {e.value: e for e in EventType}
                bad = [t for t in types if t not in valid]
                if bad:
                    raise ToolError(f"unknown event types {bad}; valid: {sorted(valid)}")
                wanted = {valid[t] for t in types}
            return state.out(analytics.get_events(s, dev, a, b, wanted, max(1, min(limit, 1000)), ctx))

    @server.tool()
    @_guard
    def segment_stats(
        date: str, start: str, end: str, channels: list[str] | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """Statistics (min/p5/p50/mean/p95/max, coverage) of channels and the events inside a
        time window, e.g. date=2026-11-08 start=02:00 end=02:30 (local time within the therapy day)."""
        with state.store() as s:
            dev = state.device(s, device_id)
            d = state.day(s, dev, date)
            a, b = state.window(d, start, end)
            return state.out(analytics.segment_stats(s, dev, a, b, ctx, channels))

    @server.tool()
    @_guard
    def get_signal(
        date: str, start: str, end: str, channel: str, max_points: int = 300, device_id: str | None = None
    ) -> dict[str, Any]:
        """A channel downsampled to at most `max_points` (hard cap 2000) buckets of min/mean/max.
        `flow` and `pressure_hr` only for windows up to 30 min."""
        with state.store() as s:
            dev = state.device(s, device_id)
            d = state.day(s, dev, date)
            a, b = state.window(d, start, end)
            if channel not in CHANNELS and channel not in CHANNEL_ALIASES:
                have = sorted({c for x in s.sessions(dev, d, d) for c in x.channels})
                raise ToolError(f"unknown channel {channel!r}; channels available on {d}: {have}")
            return state.out(analytics.get_signal(s, dev, a, b, channel, max_points))

    @server.tool()
    @_guard
    def get_signals(
        date: str, start: str, end: str, channels: list[str], device_id: str | None = None
    ) -> dict[str, Any]:
        """Raw samples of several channels (e.g. flow, pressure_hr, mask_pressure, pressure, spo2) on
        ONE time grid, no aggregation and no interpolation. The grid step is that of the finest
        channel (25 Hz -> 0.04 s); a slower channel has its real sample on matching rows and null
        elsewhere. Row i is at `start + i * step_s` (no time list). Limit: 2000 rows (80 s with a
        25 Hz channel; slow channels alone can span much longer). For longer windows use
        `get_signal` (aggregated)."""
        with state.store() as s:
            dev = state.device(s, device_id)
            d = state.day(s, dev, date)
            a, b = state.window(d, start, end)
            known = set(CHANNELS) | set(CHANNEL_ALIASES)
            bad = [c for c in channels if c not in known]
            if bad:
                have = sorted({c for x in s.sessions(dev, d, d) for c in x.channels})
                raise ToolError(f"unknown channels {bad}; channels available on {d}: {have}")
            return state.out(analytics.get_signals(s, dev, a, b, channels))

    @server.tool()
    @_guard
    def oximetry_summary(
        date: str, start: str | None = None, end: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """SpO2/pulse metrics (mean, min, T90, ODI3/4, pulse) with artefact filtering and the
        desaturation list for a night or a window. `available=false` when there is no valid SpO2."""
        with state.store() as s:
            dev = state.device(s, device_id)
            d = state.day(s, dev, date)
            a, b = state.window(d, start, end)
            res = ox.oximetry_summary(s, dev, a, b, ctx.thresholds)
            if res is None:
                return {"available": False, "notes": ["no valid SpO2 data in this window"]}
            return {"available": True, **state.out(res)}

    @server.tool()
    @_guard
    def disturbance_summary(
        date: str, start: str | None = None, end: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """Sleep disturbances beyond AHI for a night or a window: `measured` (pulse rate response
        to events, rapid pulse rises with their breathing response, hypoxic burden, flow limitation,
        calm vs restless breathing, inspiratory flow shape NED/Glasgow Index) and `inferred`
        (possible autonomic arousals, RERA candidates from device flow limitation and from NED)
        metrics, `flow_shape_by_ps` (flow shape split by pressure support 0 / > 0), plus timestamped
        `findings` (incl. restless breathing periods) and per-event pulse responses. Inferred items
        are not scored arousals/RERAs; calm/restless is not sleep staging."""
        with state.store() as s:
            dev = state.device(s, device_id)
            d = state.day(s, dev, date)
            a, b = state.window(d, start, end)
            return state.out(dist.disturbance_summary(s, dev, a, b, ctx.thresholds))

    @server.tool()
    @_guard
    def disturbance_trend(
        date_from: str | None = None, date_to: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """Nightly disturbance metrics (PRR, pulse-rise index, hypoxic burden, flow limitation,
        calm-breathing share, flow shape: NED and Glasgow Index, inferred arousal, NED RERA and
        RERA-candidate indices) one row per night, with
        units/methods and the mean per metric. Default: the last 14 days of data. Null = signal
        missing that night."""
        with state.store() as s:
            dev = state.device(s, device_id)
            try:
                end = analytics.parse_day(date_to or "last", ctx, s, dev)
                start = analytics.parse_day(date_from, ctx, s, dev) if date_from else end - timedelta(days=13)
            except AnalyticsError as exc:
                raise ToolError(str(exc)) from None
            if start > end:
                raise ToolError("date_from must not be after date_to")
            return state.out(dist.disturbance_trend(s, dev, start, end, ctx))

    @server.tool()
    @_guard
    def compare_periods(
        a_from: str, a_to: str, b_from: str, b_to: str, device_id: str | None = None,
        include_disturbances: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        """Mean nightly metrics for period A and B with absolute and relative delta (B − A).
        `include_disturbances=true` adds the nightly `disturbance_trend` metrics (pulse, flow
        limitation, breathing stability, flow shape NED/Glasgow, RERA candidates), e.g. to compare
        nights before and after a pressure change; slower (about 1-3 s per night)."""
        with state.store() as s:
            dev = state.device(s, device_id)
            try:
                days = [analytics.parse_day(x, ctx, s, dev) for x in (a_from, a_to, b_from, b_to)]
            except AnalyticsError as exc:
                raise ToolError(str(exc)) from None
            if days[0] > days[1] or days[2] > days[3]:
                raise ToolError("period start must not be after its end")
            a = analytics.list_nights(s, dev, days[0], days[1], ctx)
            b = analytics.list_nights(s, dev, days[2], days[3], ctx)
            if not include_disturbances:
                return state.out(analytics.compare_periods(a, b))
            ta = dist.disturbance_trend(s, dev, days[0], days[1], ctx)
            tb = dist.disturbance_trend(s, dev, days[2], days[3], ctx)
            units = {k: m["unit"] for k, m in (ta["metrics"] | tb["metrics"]).items()}
            return state.out(analytics.compare_periods(a, b, ta["nights"], tb["nights"], units))

    @server.tool()
    @_guard
    def get_settings(date: str | None = None, device_id: str | None = None) -> dict[str, Any]:
        """Device settings: those in effect on `date`, or the full history of change points."""
        with state.store() as s:
            dev = state.device(s, device_id)
            history = analytics.settings_history(s, dev)
            if date is None:
                return state.out({"device_id": dev, "history": history})
            try:
                d = analytics.parse_day(date, ctx, s, dev)
            except AnalyticsError as exc:
                raise ToolError(str(exc)) from None
            cur = [h for h in history if h["valid_from"] <= d]
            mode, _ = night_mode(s.sessions(dev, d, d))
            if not cur:
                return state.out({"device_id": dev, "date": d, "session_mode": mode, "in_effect": None,
                                  "notes": ["no settings known for this day"]})  # fmt: skip
            _, notes = settings_in_effect(s, dev, d, mode)
            in_effect = {**cur[-1], "applies": False} if notes else cur[-1]
            return state.out({"device_id": dev, "date": d, "session_mode": mode, "in_effect": in_effect,
                              "notes": notes})  # fmt: skip

    if not read_only:

        @server.tool()
        def ingest(source: str | None = None) -> dict[str, Any]:
            """Admin: import new data from the configured sources (or one directory) into the store."""
            from pathlib import Path

            from pap_analytics.ingest import ingest as run_ingest

            try:
                reports = run_ingest(config, source=Path(source) if source else None)
            except StoreLockedError as exc:
                raise ToolError(f"another ingest is running: {exc}") from None
            return state.out({"reports": reports})

    @server.resource("pap://glossary", mime_type="text/markdown")
    def glossary() -> str:
        """Channels, units, event types, provenance and the therapy-day definition."""
        return GLOSSARY

    @server.resource("pap://devices", mime_type="application/json")
    def devices_resource() -> str:
        import json

        with state.store() as s:
            return json.dumps(to_dict(s.devices(), ctx.tz), ensure_ascii=False)

    @server.prompt()
    def nightly_review(date: str = "last") -> str:
        """Review one night."""
        return (
            f"Review the PAP therapy night {date}.\n"
            f"1. Call `night_summary(date={date!r})`.\n"
            "2. If a `find_anomalies` tool is available, call it for the same date and look at the "
            "top findings with `segment_stats` / `get_events` on the suspicious windows.\n"
            f"3. For sleep quality beyond AHI call `disturbance_summary(date={date!r})`; report "
            "`measured` and `inferred` metrics separately and call inferred arousals/RERAs "
            "'possible', never scored events.\n"
            "4. Always say which values are `device` and which are `derived` (provenance); "
            "mention notes about missing data.\n"
            f"5. End with the disclaimer: {DISCLAIMER}"
        )

    import pap_analytics.mcp_extras  # noqa: F401  (registers find_anomalies / plot_segment)

    for register in _extensions:
        register(server, state)
    return server
