"""`pap sync` and `pap config show` subcommands."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pap_analytics import cli
from pap_analytics.config import Config
from pap_analytics.sync import SyncError, sync_all


def _sync_cmd(args: argparse.Namespace, config: Config) -> int:
    try:
        results = sync_all(config, args.source)
    except SyncError as exc:
        print(f"ERROR: {exc}")
        return 2
    for r in results:
        if r.ok:
            print(f"{r.source}: synced, {r.changed} changes")
        else:
            age = (
                f", data age {r.data_age_s / 3600:.1f} h" if r.data_age_s is not None else ", no earlier copy"
            )
            print(f"{r.source}: SYNC FAILED ({r.error}); keeping last good copy{age}")
    return 0 if all(r.ok for r in results) else 1


def _file_status(p: Path | None) -> str:
    if p is None:
        return "not configured"
    if not p.is_file():
        return "MISSING"
    return "ok (readable)" if os.access(p, os.R_OK) else "NOT READABLE"


def _file_info(p: Path | None) -> dict[str, str | None]:
    return {"path": str(p) if p else None, "status": _file_status(p)}


def effective_config(config: Config) -> dict[str, Any]:
    """Effective config; secret files are reported by status only, never by content."""
    return {
        "store_dir": str(config.store_dir),
        "day_boundary_hour": config.day_boundary_hour,
        "sources": [
            {
                "name": s.name, "path": str(s.path), "path_exists": s.path.is_dir(),
                "adapter": s.adapter, "timezone": s.timezone,
                "sync": None if s.sync is None else {
                    "type": s.sync.type, "remote": s.sync.remote, "interval_s": s.sync.interval_s,
                    "ssh_key_file": _file_info(s.sync.ssh_key_file),
                    "known_hosts_file": _file_info(s.sync.known_hosts_file),
                },
            }
            for s in config.sources
        ],
        "http": {
            "host": config.http.host, "port": config.http.port,
            "token_file": _file_info(config.http.token_file),
        },
        "thresholds": effective_thresholds(config.thresholds),
    }  # fmt: skip


def effective_thresholds(overrides: dict[str, Any]) -> dict[str, float]:
    from pap_analytics.analytics.thresholds import effective

    return effective(overrides)

def _config_cmd(args: argparse.Namespace, config: Config) -> int:
    print(json.dumps(effective_config(config), indent=2))
    return 0


def register(subparsers: Any) -> None:
    p = subparsers.add_parser("sync", help="pull source data from its remote (rsync)")
    p.add_argument("--source", metavar="NAME", help="only this source (by name)")
    p.set_defaults(func=_sync_cmd)
    c = subparsers.add_parser("config", help="inspect configuration")
    csub = c.add_subparsers(dest="config_command", required=True)
    show = csub.add_parser("show", help="effective config, secrets redacted")
    show.set_defaults(func=_config_cmd)


cli.add_command(register)
