"""`pap ingest` and `pap doctor` subcommands."""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from pap_analytics import adapters, cli
from pap_analytics.config import Config, SourceConfig
from pap_analytics.ingest import ingest
from pap_analytics.sync import sleep_interval, sync_all


def _ingest_cmd(args: argparse.Namespace, config: Config) -> int:
    def run() -> int:
        sync_failed = False
        if not args.no_sync and not args.source:
            for r in sync_all(config):
                if not r.ok:
                    sync_failed = True
                    age = f"{r.data_age_s / 3600:.1f} h" if r.data_age_s is not None else "unknown"
                    print(f"warning: sync of {r.source} failed ({r.error}); last good copy, age {age}")
        reports = ingest(
            config,
            source=Path(args.source) if args.source else None,
            rebuild=args.rebuild,
            prune=args.prune,
        )
        if args.json:
            print(json.dumps([dataclasses.asdict(r) for r in reports], indent=2))
        else:
            for r in reports:
                print(
                    f"{r.source}: device={r.device_id} added={r.sessions_added} updated={r.sessions_updated} "
                    f"unchanged={r.sessions_unchanged} failed={r.sessions_failed} days={r.therapy_days} "
                    f"({r.duration_s:.1f}s)"
                )
                for w in r.warnings:
                    print(f"  warning: {w}")
                for e in r.errors:
                    print(f"  ERROR: {e}")
        return 1 if any(r.errors for r in reports) or sync_failed else 0

    if args.watch is None:
        return run()
    interval = sleep_interval(config) if args.watch < 0 else args.watch
    while True:  # simple sleep loop for Docker
        run()
        time.sleep(interval)


def doctor_report(path: Path, tz_name: str = "Europe/Prague", boundary: int = 12) -> dict[str, Any]:
    src = SourceConfig(path=path, timezone=tz_name)
    adapter = adapters.detect(path)
    device = adapter.device(path)
    refs = adapter.scan(path, src.tz, boundary)
    channels_by_type: dict[str, set[str]] = defaultdict(set)
    oximetry: dict[str, bool] = {}
    days: set[str] = set()
    adapter.warnings.clear()
    for ref in refs:
        s = adapter.load_session(path, ref, src.tz, boundary)
        day = ref.therapy_day.isoformat()
        days.add(day)
        spo2 = next((c for c in s.channels if c.name == "spo2"), None)
        oximetry[day] = oximetry.get(day, False) or bool(spo2 is not None and np.isfinite(spo2.values).any())
        for f in ref.files:
            kind = f.stem.rsplit("_", 1)[-1]
            channels_by_type[kind].update(c.name for c in s.channels if _from_kind(c.name, kind))
    td = adapter.therapy_days(path, src.tz)
    return {
        "adapter": adapter.name,
        "device": dataclasses.asdict(device),
        "days": len(days),
        "sessions": len(refs),
        "channels_by_file_type": {k: sorted(v) for k, v in sorted(channels_by_type.items())},
        "oximetry_days": sorted(d for d, ok in oximetry.items() if ok),
        "str_range": [td[0].date.isoformat(), td[-1].date.isoformat()] if td else None,
        "str_days": len(td),
        "warnings": list(dict.fromkeys(adapter.warnings)),
    }


_KIND_CHANNELS = {
    "BRP": {"flow", "pressure_hr"},
    "SA2": {"spo2", "pulse"},
}


def _from_kind(channel: str, kind: str) -> bool:
    if kind == "PLD":
        return channel not in _KIND_CHANNELS["BRP"] | _KIND_CHANNELS["SA2"]
    return channel in _KIND_CHANNELS.get(kind, set())


def _doctor_cmd(args: argparse.Namespace, config: Config) -> int:
    path = Path(args.path)
    tz = next((s.timezone for s in config.sources if s.path == path), "Europe/Prague")
    try:
        rep = doctor_report(path, tz, config.day_boundary_hour)
    except adapters.UnknownSourceError as exc:
        print(f"ERROR: {exc}")
        return 2
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        d = rep["device"]
        print(f"adapter: {rep['adapter']}\ndevice: {d['model']} ({d['model_code']}) serial {d['serial']}")
        print(f"therapy days with sessions: {rep['days']}, sessions: {rep['sessions']}")
        for k, v in rep["channels_by_file_type"].items():
            print(f"  {k}: {', '.join(v) or '-'}")
        print(f"oximetry on {len(rep['oximetry_days'])} days: {', '.join(rep['oximetry_days']) or '-'}")
        print(f"STR days: {rep['str_days']} {rep['str_range'] or ''}")
        for w in rep["warnings"]:
            print(f"warning: {w}")
    return 0


def register(subparsers: Any) -> None:
    p = subparsers.add_parser("ingest", help="import source data into the store")
    p.add_argument("--source", help="ingest only this directory")
    p.add_argument("--rebuild", action="store_true", help="wipe the store first")
    p.add_argument("--prune", action="store_true", help="remove store data missing from the source")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-sync", action="store_true", help="skip the sync step of sources that have one")
    p.add_argument(
        "--watch", nargs="?", type=float, const=-1, default=None, metavar="SECONDS",
        help="repeat (sync + ingest); interval SECONDS, default the smallest sources.sync.interval_s",
    )  # fmt: skip
    p.set_defaults(func=_ingest_cmd)
    d = subparsers.add_parser("doctor", help="inspect a source directory (no store needed)")
    d.add_argument("path")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=_doctor_cmd)


cli.add_command(register)
