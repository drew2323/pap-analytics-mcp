"""Cache of nightly ``disturbance_trend`` rows in ``<store_dir>/trend_cache.json``.

Not part of the catalog and not a contract: a disposable file beside the store (like
``sync_state.json``), written by ``pap ingest`` only and read by everyone. A cached row is used only
when its key matches; the key covers the night's sessions (ids, fingerprints, completeness), the
package version and the analysis context (thresholds, day boundary, timezone). Anything else -
missing, unreadable, foreign or stale entries - falls back to live computation, so the cache can
change speed but never results.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from pap_analytics import __version__
from pap_analytics.analytics.timeparse import Context
from pap_analytics.store import SessionInfo

CACHE_FILE = "trend_cache.json"
FORMAT = 1

def night_key(sessions: Sequence[SessionInfo], ctx: Context) -> str:
    """Hash of everything a nightly trend row depends on."""
    parts = {
        "format": FORMAT,
        "version": __version__,
        "sessions": sorted((s.id, s.fingerprint, s.complete) for s in sessions),
        "tz": str(ctx.tz),
        "boundary": ctx.boundary_hour,
        "thresholds": sorted((k, float(v)) for k, v in ctx.thresholds.items()),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

def cacheable(sessions: Sequence[SessionInfo]) -> bool:
    """Every night with sessions is cached. A growing session changes its fingerprint on every
    re-ingest, which changes the key, so a cached row always equals a live computation on the same
    store contents (``complete`` is not relied on: it can stay false after a session stopped growing)."""
    return bool(sessions)

def read(store_dir: Path) -> dict[str, Any]:
    """``{device_id: {date: {"key", "row", "meta"}}}``; empty on a missing or unreadable file."""
    try:
        data = json.loads((Path(store_dir) / CACHE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return {}
    devices = data.get("devices")
    return devices if isinstance(devices, dict) else {}

def lookup(cache: dict[str, Any], device_id: str, day: date, key: str) -> tuple[dict, dict] | None:
    """Cached ``(row, meta)`` for the night when its key matches, else None."""
    entry = cache.get(device_id, {}).get(day.isoformat())
    if not isinstance(entry, dict) or entry.get("key") != key:
        return None
    row, meta = entry.get("row"), entry.get("meta")
    if not isinstance(row, dict) or not isinstance(meta, dict):
        return None
    return {**row, "date": day}, meta

def write(store_dir: Path, devices: dict[str, Any]) -> None:
    """Atomically replace the cache file (temp file + rename)."""
    store_dir = Path(store_dir)
    tmp = store_dir / (CACHE_FILE + ".tmp")
    tmp.write_text(json.dumps({"format": FORMAT, "devices": devices}, default=str))
    os.replace(tmp, store_dir / CACHE_FILE)
