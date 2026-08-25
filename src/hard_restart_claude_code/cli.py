from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .restart import (
    PROFILE_SOURCE_EXPLICIT,
    PROFILE_SOURCE_NONE,
    ProfileChoice,
    ProfileDirError,
    discover_exe,
    hard_restart,
    validate_profile_dir,
)

EXIT_OK = 0
EXIT_NO_EXE = 2
EXIT_BAD_PROFILE_DIR = 3
EXIT_CONTRADICTORY_FLAGS = 4

NO_EXE_MESSAGE = (
    "error: could not resolve a single Claude Desktop install - either none was "
    "found, or more than one package is present (which happens mid-update). "
    "Pass --exe to point at claude.exe."
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profile = resolve_profile_choice(args)
    except ProfileDirError as err:
        return fail(f"error: {err}", EXIT_BAD_PROFILE_DIR, as_json=args.json)
    except ContradictoryFlags as err:
        return fail(f"error: {err}", EXIT_CONTRADICTORY_FLAGS, as_json=args.json)

    exe = args.exe or discover_exe()
    if exe is None:
        return fail(NO_EXE_MESSAGE, EXIT_NO_EXE, as_json=args.json)
    try:
        result = hard_restart(
            exe, dry_run=args.dry_run, no_launch=args.no_launch, profile=profile
        )
    except FileNotFoundError as err:
        return fail(f"error: {err}", EXIT_NO_EXE, as_json=args.json)

    if args.json:
        print(json.dumps(result_as_dict(result, dry_run=args.dry_run), indent=2))
    else:
        print_result(result, dry_run=args.dry_run)
    return EXIT_OK


class ContradictoryFlags(ValueError):
    pass


def resolve_profile_choice(args) -> ProfileChoice:
    if args.no_profile:
        return ProfileChoice(bare=True)
    if args.profile_dir is None:
        return ProfileChoice()
    if args.no_launch:
        raise ContradictoryFlags(
            "--profile-dir asks for a relaunch and --no-launch forbids one"
        )
    return ProfileChoice(explicit=validate_profile_dir(args.profile_dir))


def fail(message: str, code: int, *, as_json: bool) -> int:
    print(message, file=sys.stderr)
    if as_json:
        print(json.dumps({"error": message, "exit_code": code}, indent=2))
    return code


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
    add_profile_flags(parser)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON. Prefer this to parsing the prose output.",
    )
    return parser


def add_profile_flags(parser: argparse.ArgumentParser) -> None:
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument(
        "--profile-dir",
        default=None,
        metavar="DIR",
        help=(
            "Relaunch against this --user-data-dir instead of the one that was "
            "running. Every matching Desktop process is still killed, whatever "
            "profile it was on."
        ),
    )
    profile.add_argument(
        "--no-profile",
        action="store_true",
        help="Relaunch bare, discarding the profile that was in use.",
    )


def result_as_dict(result, *, dry_run: bool) -> dict:
    return {
        "killed": result.killed,
        "launched": result.launched,
        "exe": str(result.exe),
        "dry_run": dry_run,
        "observed_profile": result.profile_dir,
        "observed_profile_conflict": result.profile_conflict,
        "launch_profile_dir": result.launch_profile_dir,
        "profile_source": result.profile_source,
    }


def print_result(result, *, dry_run: bool) -> None:
    if result.killed:
        print(f"matched pids: {', '.join(str(p) for p in result.killed)}")
    else:
        print("matched pids: none")
    print_profile(result, dry_run=dry_run)
    if dry_run:
        print("dry-run: nothing killed, nothing launched")
        return
    if result.launched:
        print(f"launched: {result.exe}")
    else:
        print("launch skipped")


def print_profile(result, *, dry_run: bool) -> None:
    if result.profile_source == PROFILE_SOURCE_EXPLICIT:
        print_explicit_profile(result, dry_run=dry_run)
        return
    if not result.killed:
        return
    if result.profile_source == PROFILE_SOURCE_NONE:
        print("profile: relaunching bare (--no-profile)")
        return
    if not result.launch_profile_dir:
        print("profile: default (no --user-data-dir in use)")
        return
    verb = "would preserve" if dry_run else "preserving"
    print(f"profile: {verb} --user-data-dir={result.launch_profile_dir}")
    if result.profile_conflict:
        print(
            "warning: more than one profile is running; only the one above "
            "will be relaunched",
            file=sys.stderr,
        )


# With an explicit dir hrcc made no choice, so several running profiles is
# information about what was killed, not a warning about a guess it made.
def print_explicit_profile(result, *, dry_run: bool) -> None:
    verb = "would use" if dry_run else "using"
    print(f"profile: {verb} --user-data-dir={result.launch_profile_dir} (explicit)")
    if result.profile_conflict:
        fate = "would all be killed" if dry_run else "were all killed"
        print(
            f"note: more than one profile was running; they {fate}",
            file=sys.stderr,
        )
