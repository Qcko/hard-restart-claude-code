from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .lock import RunLock, acquire_run_lock
from .progress import PHASE_WAITING_PACKAGE, Progress, default_progress_file, open_progress
from .restart import (
    DEFAULT_WAITS,
    NO_GATE,
    PACKAGE_BUDGET_SECONDS,
    PACKAGE_STATUS_OK,
    PROFILE_SOURCE_EXPLICIT,
    PROFILE_SOURCE_NONE,
    Effects,
    PackageGate,
    ProfileChoice,
    ProfileDirError,
    RestartBlocked,
    discover_exe,
    hard_restart,
    read_packages,
    simulated_reader,
    validate_profile_dir,
)

EXIT_OK = 0
EXIT_NO_EXE = 2
EXIT_BAD_PROFILE_DIR = 3
EXIT_CONTRADICTORY_FLAGS = 4
EXIT_CANNOT_CONFIRM = 5
EXIT_ALREADY_RUNNING = 6

BUSY_MESSAGE = (
    "error: another hardened restart is already in progress. Starting a second "
    "one would kill the Claude Desktop the first is still bringing up."
)

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

    lock = take_run_lock(args)
    if lock.busy:
        return fail(BUSY_MESSAGE, EXIT_ALREADY_RUNNING, as_json=args.json)
    try:
        return run_restart(args, exe, profile)
    finally:
        lock.release()


# Only the hardened path takes it, and --dry-run never does. A bare hrcc is a
# one-second kill-and-relaunch that has never coordinated with anything, and
# making it start refusing would change the one command this project promises
# to leave alone.
def take_run_lock(args) -> RunLock:
    if args.dry_run or not wants_verification(args):
        return RunLock()
    return acquire_run_lock()


def run_restart(args, exe, profile) -> int:
    progress = build_progress(args)
    try:
        result = hard_restart(
            exe,
            dry_run=args.dry_run,
            no_launch=args.no_launch,
            profile=profile,
            gate=build_gate(args, progress),
            effects=Effects(progress=progress),
        )
    except RestartBlocked as err:
        # Desktop may already be down at this point, so report what was killed:
        # "nothing happened, safe to retry" and "Desktop is down and did not come
        # back" are very different situations to be handed one exit code for.
        return fail(
            f"error: {err}",
            EXIT_CANNOT_CONFIRM,
            as_json=args.json,
            killed=err.killed,
            attempts=err.attempts,
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


# Verification is opt-in and implied by --profile-dir. A bare hrcc keeps its
# one-second kill-and-relaunch, because it is normally typed by a human from a
# shell inside Desktop and a command that can block for minutes is a different
# tool than the one they learned.
def wants_verification(args) -> bool:
    return bool(args.verify or args.profile_dir is not None or args.simulate_package_status)


def build_gate(args, progress: Progress) -> PackageGate:
    simulate = args.simulate_package_status
    if not wants_verification(args):
        return NO_GATE
    return PackageGate(
        enabled=True,
        budget_seconds=args.package_budget,
        reader=simulated_reader(simulate) if simulate else read_packages,
        on_status=lambda status: report_package_status(status, progress),
    )


# The gate owns the only phase hard_restart cannot publish for itself, because
# the status being waited on is the gate's to report.
def report_package_status(status: str, progress: Progress) -> None:
    warn(f"package: {status}")
    progress.publish(
        PHASE_WAITING_PACKAGE, "Waiting for the Claude package", packageStatus=status
    )


# The default lives in hrcc's OWN directory. Hardcoding a consumer's path would
# put that consumer's name in this tool's source, which is the dependency
# inversion the profile design already refuses. A caller passes its own path.
def build_progress(args) -> Progress:
    verifying = wants_verification(args)
    file = args.progress_file
    if args.dry_run:
        # A dry run changes nothing, and a state file is a change.
        file = None
    elif file is None and verifying:
        file = default_progress_file()
    attempts = DEFAULT_WAITS.launch_attempts if verifying else 1
    return open_progress(file, label=args.label, max_attempts=attempts, log=warn)


# hrcc is usually orphaned by the very kill it performs, so its stderr is a dead
# pipe for most of a hardened run. A diagnostic must never be the thing that
# takes the restart down.
def warn(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def fail(
    message: str,
    code: int,
    *,
    as_json: bool,
    killed: list[int] | None = None,
    attempts: int = 0,
) -> int:
    print(message, file=sys.stderr)
    if killed:
        print(f"killed before stopping: {', '.join(str(p) for p in killed)}", file=sys.stderr)
    if as_json:
        payload = {
            "error": message,
            "exit_code": code,
            "killed": killed or [],
            "attempts": attempts,
        }
        print(json.dumps(payload, indent=2))
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
    add_verify_flags(parser)
    add_progress_flags(parser)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON. Prefer this to parsing the prose output.",
    )
    return parser


# hrcc is a writer of progress and never a UI owner. --label is an opaque
# display string precisely so this tool never learns what the caller thinks it
# is restarting for; if anyone proposes --account, the boundary has leaked.
def add_progress_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Publish phase transitions here as JSON, with an append-only trace "
            "beside it. Defaults to hrcc's own directory when verifying."
        ),
    )
    parser.add_argument(
        "--label",
        default=None,
        metavar="TEXT",
        help="An opaque display string echoed into the progress file.",
    )


def add_verify_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Wait for the Claude package to be serviceable before launching. "
            "Implied by --profile-dir. Off by default so a bare restart stays fast."
        ),
    )
    parser.add_argument(
        "--package-budget",
        type=float,
        default=PACKAGE_BUDGET_SECONDS,
        metavar="SECONDS",
        help="How long to wait for the package before launching anyway.",
    )
    parser.add_argument(
        "--simulate-package-status",
        default=None,
        type=simulatable_status,
        metavar="STATUS",
        help=(
            "Pretend the package reports STATUS (e.g. Disabled, or 'unreadable') "
            "instead of asking Windows. For exercising the wait without an update."
        ),
    )


# A simulated package has no executable on disk, so it can never be launched -
# which is the right safety answer, but it means simulating Ok would silently
# poll to budget-exhausted instead of the success it looks like it is asking for.
def simulatable_status(value: str) -> str:
    if value.casefold() == PACKAGE_STATUS_OK.casefold():
        raise argparse.ArgumentTypeError(
            "cannot simulate Ok - that is the real state; use --verify on its own"
        )
    return value


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
        "package_status": result.package_status,
        "attempts": result.attempts,
    }


def print_result(result, *, dry_run: bool) -> None:
    if result.killed:
        print(f"matched pids: {', '.join(str(p) for p in result.killed)}")
    else:
        print("matched pids: none")
    print_profile(result, dry_run=dry_run)
    if result.package_status:
        print(f"package: {result.package_status}")
    if dry_run:
        print("dry-run: nothing killed, nothing launched")
        return
    if result.launched:
        print(f"launched: {result.exe}")
        if result.attempts > 1:
            print(f"attempts: {result.attempts}")
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
