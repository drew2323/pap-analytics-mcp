"""`pap` command line entry point.

Each work package adds its subcommand via a ``register(subparsers)`` function and
appends it with :func:`add_command`; the parser built here runs all of them.
Subcommand handlers set ``set_defaults(func=handler)`` where ``handler(args, config) -> int``.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from typing import Any

from pap_analytics import __version__
from pap_analytics.config import load_config

Registrar = Callable[[Any], None]  # receives the argparse subparsers action

_REGISTRARS: list[Registrar] = []


def add_command(registrar: Registrar) -> None:
    """Register a ``register(subparsers)`` function (idempotent)."""
    if registrar not in _REGISTRARS:
        _REGISTRARS.append(registrar)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pap", description="PAP therapy data analytics")
    parser.add_argument("--version", action="version", version=f"pap-analytics-mcp {__version__}")
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="config TOML (default: $PAP_CONFIG or ~/.config/pap-analytics/config.toml)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="logging level (default: WARNING)",
    )
    import pap_analytics.cli_ingest  # noqa: F401  (registers subcommands)
    import pap_analytics.cli_query  # noqa: F401

    subparsers = parser.add_subparsers(dest="command")
    for registrar in _REGISTRARS:
        registrar(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    if args.command is None:
        parser.print_help()
        return 0
    func = getattr(args, "func", None)
    if func is None:
        parser.error(f"command {args.command!r} has no handler")
    return int(func(args, load_config(args.config)))


if __name__ == "__main__":
    raise SystemExit(main())
