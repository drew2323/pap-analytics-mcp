"""`pap` command line entry point. Subcommands are added by the work packages."""

from __future__ import annotations

import argparse

from pap_analytics import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pap", description="PAP therapy data analytics")
    parser.add_argument("--version", action="version", version=f"pap-analytics-mcp {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
