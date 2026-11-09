"""`pap nights`, `pap night`, `pap segment`: query the store from the shell (JSON or text)."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from typing import Any

from pap_analytics import cli
from pap_analytics.analytics import (
    AnalyticsError,
    Context,
    list_nights,
    night_summary,
    parse_day,
    parse_time,
    segment_stats,
    to_dict,
)
from pap_analytics.analytics.timeparse import resolve_device
from pap_analytics.config import Config
from pap_analytics.store import Store


def _run(args: argparse.Namespace, config: Config, fn: Any) -> int:
    ctx = Context.from_config(config)
    try:
        with Store.open(config.store_dir, read_only=True) as store:
            device = resolve_device(store, args.device)
            result = fn(store, device, ctx)
    except (AnalyticsError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}")
        return 2
    data = to_dict(result, ctx.tz)
    print(json.dumps(data, indent=None if args.json else 2, ensure_ascii=False))
    return 0


def _nights(args: argparse.Namespace, config: Config) -> int:
    def fn(store: Store, dev: str, ctx: Context) -> Any:
        end = parse_day(args.to, ctx, store, dev)
        start = parse_day(args.start, ctx, store, dev) if args.start else end - timedelta(days=args.days - 1)
        return list_nights(store, dev, start, end, ctx)

    return _run(args, config, fn)


def _night(args: argparse.Namespace, config: Config) -> int:
    return _run(
        args, config, lambda st, dev, ctx: night_summary(st, dev, parse_day(args.day, ctx, st, dev), ctx)
    )


def _segment(args: argparse.Namespace, config: Config) -> int:
    def fn(store: Store, dev: str, ctx: Context) -> Any:
        day = parse_day(args.day, ctx, store, dev)
        return segment_stats(
            store, dev, parse_time(args.start, day, ctx), parse_time(args.end, day, ctx), ctx
        )

    return _run(args, config, fn)


def register(subparsers: Any) -> None:
    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--device", help="device id (optional if only one)")
        p.add_argument("--json", action="store_true", help="compact one-line JSON")

    p = subparsers.add_parser("nights", help="one row per night")
    p.add_argument("--from", dest="start", help="first day (YYYY-MM-DD)")
    p.add_argument("--to", default="last", help="last day (YYYY-MM-DD|last|yesterday), default last")
    p.add_argument("--days", type=int, default=14, help="number of days when --from is omitted")
    common(p)
    p.set_defaults(func=_nights)
    p = subparsers.add_parser("night", help="detail of one night")
    p.add_argument("day", nargs="?", default="last")
    common(p)
    p.set_defaults(func=_night)
    p = subparsers.add_parser("segment", help="statistics of a time window within a therapy day")
    p.add_argument("day")
    p.add_argument("start", help="HH:MM or ISO datetime")
    p.add_argument("end", help="HH:MM or ISO datetime")
    common(p)
    p.set_defaults(func=_segment)


cli.add_command(register)
