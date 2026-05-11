from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .restart import discover_exe, hard_restart


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    exe = args.exe or discover_exe()
    if exe is None:
        print(
            "error: could not locate Claude Desktop install under any "
            "WindowsApps directory. Pass --exe to point at claude.exe.",
            file=sys.stderr,
        )
        return 2
    try:
        result = hard_restart(exe, dry_run=args.dry_run, no_launch=args.no_launch)
    except FileNotFoundError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print_result(result, dry_run=args.dry_run)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hard-restart-claude-code",
        description="Kill all Claude Desktop processes and relaunch the app.",
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=None,
        help="Path to claude.exe (default: auto-discover newest Claude_* under WindowsApps).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching processes without killing or launching.",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Kill matching processes but skip relaunch.",
    )
    return parser


def print_result(result, *, dry_run: bool) -> None:
    if result.killed:
        print(f"matched pids: {', '.join(str(p) for p in result.killed)}")
    else:
        print("matched pids: none")
    if dry_run:
        print("dry-run: nothing killed, nothing launched")
        return
    if result.launched:
        print(f"launched: {result.exe}")
    else:
        print("launch skipped")
