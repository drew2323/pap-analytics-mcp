"""Anomaly and plot tools for the MCP server (registered through ``mcp_server.add_extension``)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from pap_analytics import analytics
from pap_analytics.analytics import AnalyticsError, plot
from pap_analytics.mcp_server import ServerState, add_extension

MAX_REASON_NOTE = "Rule-based pointers (derived), not diagnoses; inspect with the suggested_call."


def register_tools(server: FastMCP, state: ServerState) -> None:
    ctx = state.ctx

    @server.tool()
    def find_anomalies(date: str, device_id: str | None = None) -> dict[str, Any]:
        """Parts of a night worth a look (event clusters, large leak, desaturations, pressure at max,
        low ventilation, mask-off gaps, metric outliers vs the last 14 nights), most severe first,
        each with a plain-English reason and a `suggested_call` to inspect it."""
        try:
            with state.store() as s:
                dev = state.device(s, device_id)
                d = state.day(s, dev, date)
                found = analytics.find_anomalies(s, dev, d, ctx.thresholds, ctx)
        except AnalyticsError as exc:
            raise ToolError(str(exc)) from None
        notes = [MAX_REASON_NOTE] + ([] if found else ["no anomalies found by the rules"])
        return state.out({"date": d, "device_id": dev, "anomalies": found, "notes": notes})

    if plot.available():

        @server.tool()
        def plot_segment(
            date: str, start: str, end: str, channels: list[str] | None = None, device_id: str | None = None
        ) -> Image:
            """PNG plot of a night segment (pressure, leak, minute ventilation, SpO2 by default) with
            events as shaded bands. Use for a visual check of a window found by `find_anomalies`."""
            try:
                with state.store() as s:
                    dev = state.device(s, device_id)
                    d = state.day(s, dev, date)
                    a, b = state.window(d, start, end)
                    return Image(data=plot.plot_segment(s, dev, a, b, channels, ctx), format="png")
            except AnalyticsError as exc:
                raise ToolError(str(exc)) from None


add_extension(register_tools)
