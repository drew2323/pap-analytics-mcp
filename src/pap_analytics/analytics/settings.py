"""Device settings history (change points)."""

from __future__ import annotations

from typing import Any

from pap_analytics.store import Store


def settings_history(store: Store, device_id: str) -> list[dict[str, Any]]:
    """Each entry is a change point: ``valid_from`` plus mode, values and the keys that changed."""
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for s in store.settings(device_id):
        cur = {"mode": s.mode, **s.values}
        changed = sorted(k for k in cur if prev is None or prev.get(k) != cur[k])
        out.append({"valid_from": s.valid_from, "mode": s.mode, "values": s.values, "changed": changed})
        prev = cur
    return out
