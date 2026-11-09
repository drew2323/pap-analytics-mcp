"""Smoke helper: call night_summary(2026-11-08) over HTTP or stdio and print the device AHI.

Usage: smoke_client.py URL TOKEN | smoke_client.py stdio CMD [ARGS...]
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


async def run(session: ClientSession) -> None:
    await session.initialize()
    res = await session.call_tool("night_summary", {"date": "2026-11-08"})
    if res.isError:
        raise SystemExit(f"tool error: {res.content[0].text}")
    ahi = res.structuredContent["device_metrics"]["ahi"]
    print(ahi["value"])


async def main() -> None:
    if sys.argv[1] == "stdio":
        params = StdioServerParameters(command=sys.argv[2], args=sys.argv[3:], env=dict(os.environ))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await run(s)
    else:
        headers = {"Authorization": f"Bearer {sys.argv[2]}"}
        async with streamablehttp_client(sys.argv[1], headers=headers) as (r, w, _), ClientSession(r, w) as s:
            await run(s)


asyncio.run(main())
