from __future__ import annotations

import json
import os
import re
import subprocess
import time
import winreg
from collections.abc import Callable, Sequence
from typing import NoReturn
from dataclasses import dataclass, field
from pathlib import Path

from .progress import (
    NO_PROGRESS,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_LAUNCHING,
    PHASE_STOPPED,
    PHASE_STOPPING,
    PHASE_WAITING_DOWN,
    PHASE_WAITING_UP,
    Progress,
)

PROCESS_IMAGE_NAME = "claude.exe"
APPX_PACKAGE_NAME = "Claude"
PACKAGE_DIR_PREFIX = "Claude_"
WINDOWSAPPS_DIR = "WindowsApps"
DEFAULT_PROGRAM_FILES = r"C:\Program Files"
CURRENT_VERSION_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
PROGRAM_FILES_VALUE = "ProgramFilesDir"
EXE_RELATIVE = Path("app") / "claude.exe"
PROFILE_FLAG = "--user-data-dir"

MISSING_PACKAGE = "The Claude package is not available"

PACKAGE_POLL_SECONDS = 1.0
PACKAGE_BUDGET_SECONDS = 120.0
PACKAGE_STATUS_OK = "Ok"
PACKAGE_STATUS_UNREADABLE = "unreadable"
PACKAGE_STATUS_NOT_REGISTERED = "not registered"
PACKAGE_STATUS_BUDGET_SPENT = "budget-exhausted"

LAUNCH_ATTEMPTS = 8
LAUNCH_BACKOFF_SECONDS = 2.0
LAUNCH_BACKOFF_CAP_SECONDS = 8.0
UP_TIMEOUT_SECONDS = 30.0
UP_POLL_SECONDS = 2.0
EXIT_GRACE_SECONDS = 8.0

Runner = Callable[[Sequence[str]], "CompletedLike"]


@dataclass(frozen=True)
class CompletedLike:
    stdout: str = ""
    returncode: int = 0


@dataclass(frozen=True)
class ClaudeProcess:
    pid: int
    path: str = ""
    profile_dir: str | None = None


# "Nothing is running" and "the query could not answer" must never be the same
# value. Relaunching on the second is how a restart ends up with two Desktops on
# two data dirs, so the hardened path refuses to act while blind.
@dataclass(frozen=True)
class ProcessReport:
    readable: bool
    processes: tuple[ClaudeProcess, ...] = ()


# `killed` is the difference between "nothing happened, safe to retry" and
# "Desktop is down and did not come back", which a caller cannot otherwise tell
# from the exit code alone.
class RestartBlocked(RuntimeError):
    def __init__(
        self, message: str, killed: Sequence[int] = (), attempts: int = 0
    ) -> None:
        super().__init__(message)
        self.killed = list(killed)
        self.attempts = attempts


class ProfileDirError(ValueError):
    pass


PROFILE_SOURCE_EXPLICIT = "explicit"
PROFILE_SOURCE_INFERRED = "inferred"
PROFILE_SOURCE_NONE = "none"


# Explicit beats inferred, and omitting the flag is NOT the same as asking for a
# bare launch: a caller whose whole purpose is switching profiles must not
# silently relaunch the one already running because a flag went missing. Carried
# as one value object because hard_restart is already at the Rule of 7 limit.
@dataclass(frozen=True)
class ProfileChoice:
    explicit: str | None = None
    bare: bool = False

    # The CLI's mutually-exclusive group blocks this, but hard_restart is a
    # supported entry point, so an importing caller can build it directly. Both
    # readers below would silently resolve it to bare and drop the dir.
    def __post_init__(self) -> None:
        if self.explicit and self.bare:
            raise ProfileDirError(
                "a profile choice is either an explicit dir or bare, not both"
            )


INFERRED = ProfileChoice()


@dataclass(frozen=True)
class ClaudePackage:
    status: str
    location: str

    @property
    def exe(self) -> Path:
        return Path(self.location) / EXE_RELATIVE


# "The query returned nothing" and "the query could not run" are different
# answers, and a gate that confuses them waits out its whole budget on a broken
# reader instead of getting on with the launch.
@dataclass(frozen=True)
class PackageReport:
    readable: bool
    packages: tuple[ClaudePackage, ...] = ()


@dataclass(frozen=True)
class Result:
    killed: list[int]
    launched: bool
    exe: Path
    profile_dir: str | None = None
    profile_conflict: bool = False
    launch_profile_dir: str | None = None
    profile_source: str = PROFILE_SOURCE_INFERRED
    package_status: str | None = None
    attempts: int = 0


def discover_exe(runner: Runner | None = None) -> Path | None:
    runner = runner or _default_capture
    install_location = _query_install_location(runner)
    if not install_location:
        return None
    exe = install_location / EXE_RELATIVE
    return exe if exe.is_file() else None


def read_packages(runner: Runner | None = None) -> PackageReport:
    runner = runner or _default_capture
    # Status is an enum, so cast it in PowerShell rather than hoping ConvertTo-Json
    # renders it as a name instead of a number.
    cmd = _powershell(
        f"ConvertTo-Json -Depth 3 -InputObject @(Get-AppxPackage -Name "
        f"'{APPX_PACKAGE_NAME}' -ErrorAction SilentlyContinue | Select-Object "
        f"@{{Name='Status';Expression={{[string]$_.Status}}}},InstallLocation)"
    )
    completed = runner(cmd)
    if completed.returncode != 0:
        return PackageReport(readable=False)
    return PackageReport(readable=True, packages=_decode_packages(completed.stdout))


def _decode_packages(stdout: str) -> tuple[ClaudePackage, ...]:
    packages = []
    for row in _decode_rows(stdout):
        location = (row.get("InstallLocation") or "").strip()
        # A staged-but-unregistered package reports an empty location, and
        # joining that yields a RELATIVE path that would happily "exist"
        # against whatever the current directory happens to be.
        if not location:
            continue
        packages.append(
            ClaudePackage(status=(row.get("Status") or "").strip(), location=location)
        )
    return tuple(packages)


# Mid-update Get-AppxPackage can list a staged package beside the installed one.
# The FIRST serviceable package wins - unlike discover_exe, which refuses all
# ambiguity, because here an Ok package is exactly the thing being waited for
# and refusing it would strand the restart the gate exists to rescue.
def ready_package(
    packages: Sequence[ClaudePackage], install_prefix: str | None = None
) -> ClaudePackage | None:
    for package in packages:
        if package.status.casefold() != PACKAGE_STATUS_OK.casefold():
            continue
        if is_package_install_path(str(package.exe), install_prefix):
            if package.exe.is_file():
                return package
    return None


def package_status_summary(packages: Sequence[ClaudePackage]) -> str:
    if not packages:
        return PACKAGE_STATUS_NOT_REGISTERED
    return ", ".join(package.status for package in packages)


def _query_install_location(runner: Runner) -> Path | None:
    cmd = _powershell(
        f"ConvertTo-Json -Depth 3 -InputObject @(Get-AppxPackage -Name "
        f"'{APPX_PACKAGE_NAME}' -ErrorAction SilentlyContinue | "
        f"Select-Object InstallLocation)"
    )
    locations = _distinct_install_locations(runner(cmd).stdout)
    if len(locations) != 1:
        return None
    return Path(locations[0])


def _distinct_install_locations(stdout: str) -> list[str]:
    unique: list[str] = []
    for row in _decode_rows(stdout):
        location = (row.get("InstallLocation") or "").strip()
        if location and location not in unique:
            unique.append(location)
    return unique


def survey_processes(
    runner: Runner | None = None, install_prefix: str | None = None
) -> ProcessReport:
    runner = runner or _default_capture
    cmd = _powershell(
        f"ConvertTo-Json -Depth 3 -InputObject @(Get-CimInstance Win32_Process "
        f"-Filter \"Name='{PROCESS_IMAGE_NAME}'\" -ErrorAction SilentlyContinue | "
        f"Select-Object ProcessId,ExecutablePath,CommandLine)"
    )
    try:
        completed = runner(cmd)
    except Exception:
        return ProcessReport(readable=False)
    if completed.returncode != 0:
        return ProcessReport(readable=False)
    rows = _decode_rows_or_none(completed.stdout)
    if rows is None:
        return ProcessReport(readable=False)
    matched = [
        process
        for process in _processes_from_rows(rows)
        if is_package_install_path(process.path, install_prefix)
    ]
    return ProcessReport(readable=True, processes=tuple(matched))




def _processes_from_rows(rows: Sequence[dict]) -> list[ClaudeProcess]:
    processes = []
    for row in rows:
        pid = row.get("ProcessId")
        if not isinstance(pid, int):
            continue
        processes.append(
            ClaudeProcess(
                pid=pid,
                path=row.get("ExecutablePath") or "",
                profile_dir=parse_profile_dir(row.get("CommandLine") or ""),
            )
        )
    return sorted(processes, key=lambda process: process.pid)


# A matched process is not merely killed: its --user-data-dir is read back and
# handed to the relaunch, and a Claude data dir defines MCP servers as command
# lines. So this must stay a PREFIX test against an admin-only location - see
# "The matcher stays narrow, deliberately" in DESIGN.md for the full argument.
def is_package_install_path(
    executable_path: str, install_prefix: str | None = None
) -> bool:
    if not executable_path:
        return False
    prefix = install_prefix if install_prefix is not None else package_install_prefix()
    return _normalized(executable_path).startswith(_normalized(prefix))


def package_install_prefix(program_files: str | None = None) -> str:
    root = program_files or _program_files_dir()
    return str(Path(root) / WINDOWSAPPS_DIR / PACKAGE_DIR_PREFIX)


# Deliberately NOT os.environ["ProgramFiles"]: the threat model is a process
# running as this user, and that process controls the environment it launches us
# with. Anchoring on an env var would let it point the "admin-only" prefix at a
# directory it owns. HKLM is admin-write, which is the property being relied on.
def _program_files_dir() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CURRENT_VERSION_KEY) as key:
            value, _ = winreg.QueryValueEx(key, PROGRAM_FILES_VALUE)
    except OSError:
        return DEFAULT_PROGRAM_FILES
    return value if isinstance(value, str) and value else DEFAULT_PROGRAM_FILES


# normpath and casefold only: short names (PROGRA~1), extended-length prefixes
# (\?\C:) and subst drives are not resolved, so they fail CLOSED - nothing is
# matched and nothing is killed. That is an accepted non-match, not a bypass.
def _normalized(path: str) -> str:
    return os.path.normpath(path).casefold()


# None means "could not be understood", which is not the same as an empty list.
# A working query always emits at least "[]", so blank output is a broken reader
# rather than an idle machine.
def _decode_rows_or_none(stdout: str) -> list[dict] | None:
    text = stdout.strip()
    if not text:
        return None
    # strict=False because ConvertTo-Json emits raw control characters instead of
    # escaping them, and a Claude command line really does carry one: an observed
    # --desktop-managed-config held a literal \x07. Strict parsing rejects the
    # whole document over that one byte, which reads as "unreadable" and blocks
    # every restart on the machine. What is wanted here is a pid, a path and a
    # command line; a stray control byte inside one changes none of them.
    try:
        decoded = json.loads(text, strict=False)
    except ValueError:
        return None
    if isinstance(decoded, dict):
        return [decoded]
    if not isinstance(decoded, list):
        return None
    return [row for row in decoded if isinstance(row, dict)]


def _decode_rows(stdout: str) -> list[dict]:
    return _decode_rows_or_none(stdout) or []


_PROFILE_PATTERN = re.compile(
    re.escape(PROFILE_FLAG) + r'(?:\s*=\s*|\s+)("[^"]*"|\S+)'
)


def parse_profile_dir(command_line: str) -> str | None:
    match = _PROFILE_PATTERN.search(command_line)
    if not match:
        return None
    value = match.group(1).strip().strip('"').strip()
    return value or None


def distinct_profile_dirs(processes: Sequence[ClaudeProcess]) -> list[str]:
    unique: list[str] = []
    for process in processes:
        if process.profile_dir and process.profile_dir not in unique:
            unique.append(process.profile_dir)
    return unique


def selected_profile_dir(processes: Sequence[ClaudeProcess]) -> str | None:
    profiles = distinct_profile_dirs(processes)
    return profiles[0] if profiles else None


FORBIDDEN_PATH_CHARS = ("\x00", "\n", "\r")


# This value is handed to Electron as a data dir, and a Claude data dir holds
# live session credentials. A UNC path would put them on a remote share; a
# device path sidesteps normal path handling; control characters have no
# business in a directory name. Reject rather than sanitize - a caller that
# passes nonsense should hear about it, not get a quietly different directory.
def validate_profile_dir(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ProfileDirError("profile dir must not be empty")
    if any(char in text for char in FORBIDDEN_PATH_CHARS):
        raise ProfileDirError("profile dir must not contain NUL or newline characters")
    if not Path(text).is_absolute():
        raise ProfileDirError(f"profile dir must be an absolute path: {text}")
    _reject_remote(text)
    resolved = _resolve_or_reject(text)
    # Checking only the input is not enough. resolve() normalises a
    # forward-slash UNC path into the backslash form, and follows junctions, so
    # a value that looked local on the way in can come back out pointing at a
    # share. The resolved value is the one that gets launched, so it is the one
    # that has to satisfy the rule.
    _reject_remote(str(resolved))
    if resolved.exists() and not resolved.is_dir():
        raise ProfileDirError(f"profile dir is not a directory: {text}")
    return str(resolved)


REMOTE_PREFIXES = ("\\\\", "//")


def _reject_remote(text: str) -> None:
    if text.startswith(REMOTE_PREFIXES) or Path(text).drive.startswith(REMOTE_PREFIXES):
        raise ProfileDirError(
            f"profile dir must be a local path, not a UNC or device path: {text}"
        )


def _resolve_or_reject(text: str) -> Path:
    try:
        return Path(text).resolve()
    except OSError as err:
        raise ProfileDirError(f"profile dir is not a usable path: {text}") from err


def _ignore_status(_status: str) -> None:
    pass


# An MSIX update leaves the package Disabled for the second or two Windows needs
# to service it, and a launch landing in that window fails outright. Wait it out
# - but the gate FAILS OPEN: if it cannot read the package state, or the budget
# runs out, launch anyway. A check that cannot verify must never become the
# reason a working restart does not happen. Carried as one object so it costs
# hard_restart one parameter rather than six.
@dataclass(frozen=True)
class PackageGate:
    enabled: bool = False
    budget_seconds: float = PACKAGE_BUDGET_SECONDS
    poll_seconds: float = PACKAGE_POLL_SECONDS
    reader: Callable[[], PackageReport] = read_packages
    # monotonic, not wall clock: a clock adjustment mid-restart must not shorten
    # or extend the budget.
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    on_status: Callable[[str], None] = _ignore_status

    # A zero poll spins: against the real reader it hammers PowerShell for the
    # whole budget, and against an injected clock that only advances on sleep it
    # never terminates at all.
    def __post_init__(self) -> None:
        if self.enabled and self.poll_seconds <= 0:
            raise ValueError("package gate poll interval must be greater than zero")


NO_GATE = PackageGate()


# The gate's whole problem is that the state it waits for happens only during a
# real MSIX update, so the code has never run when it mattered. This makes the
# wait reproducible on demand instead of by ambush.
def simulated_reader(status: str) -> Callable[[], PackageReport]:
    def read() -> PackageReport:
        if status.casefold() == PACKAGE_STATUS_UNREADABLE.casefold():
            return PackageReport(readable=False)
        return PackageReport(
            readable=True,
            packages=(ClaudePackage(status=status, location=str(_simulated_location())),),
        )

    return read


def _simulated_location() -> Path:
    return Path(package_install_prefix() + "simulated")


def kill_pids(pids: list[int], runner: Runner | None = None) -> None:
    runner = runner or _default_capture
    for pid in pids:
        runner(["taskkill", "/F", "/PID", str(pid)])


# The verified launch needs the child's fate, not merely the fact that a spawn
# happened: "still alive but Desktop not up" is the one case where trying again
# would produce a SECOND Desktop. So the launcher seam hands back a handle.
@dataclass(frozen=True)
class LaunchHandle:
    running: Callable[[], bool]


def launch(
    exe: Path,
    profile_dir: str | None = None,
    launcher: Callable[[Path, str | None], LaunchHandle | None] | None = None,
) -> LaunchHandle | None:
    launcher = launcher or _default_launch
    return launcher(exe, profile_dir)


# The injected side effects, grouped. DESIGN.md's "inject side effects"
# principle is why they exist; keeping them in one object is what stops
# hard_restart's signature growing a parameter every time the restart learns to
# check one more thing.
@dataclass(frozen=True)
class Effects:
    finder: Callable[[], ProcessReport] = survey_processes
    killer: Callable[[list[int]], None] = kill_pids
    launcher: Callable[[Path, str | None], LaunchHandle | None] = launch
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    # A writer, never a UI owner. Publishing is a side effect of the restart in
    # exactly the way killing and launching are, which is why it lives here.
    progress: Progress = field(default_factory=lambda: NO_PROGRESS)


DEFAULT_EFFECTS = Effects()


# Grouped for the same reason as Effects, and because settle_seconds and the
# down-confirmation deadline are alternatives rather than companions: the
# hardened path replaces the blind sleep, it does not wait twice.
@dataclass(frozen=True)
class Waits:
    settle_seconds: float = 1.0
    down_timeout_seconds: float = 15.0
    down_poll_seconds: float = 0.3
    up_timeout_seconds: float = UP_TIMEOUT_SECONDS
    up_poll_seconds: float = UP_POLL_SECONDS
    # An MSIX launcher hands off and exits within a second on a GOOD launch, so
    # an exit only becomes evidence of failure once Desktop has had a few more
    # seconds to appear. Part of the rule, not a tuning constant.
    exit_grace_seconds: float = EXIT_GRACE_SECONDS
    launch_attempts: int = LAUNCH_ATTEMPTS
    launch_backoff_seconds: float = LAUNCH_BACKOFF_SECONDS
    launch_backoff_cap_seconds: float = LAUNCH_BACKOFF_CAP_SECONDS

    # Same hazard as PackageGate's poll: a zero interval hammers the reader for
    # the whole timeout, and against a clock that only advances on sleep it never
    # terminates.
    def __post_init__(self) -> None:
        if self.down_poll_seconds <= 0:
            raise ValueError("down-confirmation poll interval must be above zero")
        if self.down_timeout_seconds < 0:
            raise ValueError("down-confirmation timeout must not be negative")
        if self.up_poll_seconds <= 0:
            raise ValueError("launch-confirmation poll interval must be above zero")
        if self.launch_attempts < 1:
            raise ValueError("a launch needs at least one attempt")
        if min(self.up_timeout_seconds, self.exit_grace_seconds) < 0:
            raise ValueError("launch-confirmation waits must not be negative")
        # A negative backoff reaches the real time.sleep, which would raise from
        # inside the retry loop with Desktop already down.
        if min(self.launch_backoff_seconds, self.launch_backoff_cap_seconds) < 0:
            raise ValueError("launch backoff must not be negative")


DEFAULT_WAITS = Waits()


def hard_restart(
    exe: Path,
    *,
    dry_run: bool = False,
    no_launch: bool = False,
    waits: Waits = DEFAULT_WAITS,
    profile: ProfileChoice = INFERRED,
    gate: PackageGate = NO_GATE,
    effects: Effects = DEFAULT_EFFECTS,
) -> Result:
    report = effects.finder()
    if gate.enabled and not report.readable:
        _fail(
            effects,
            "Cannot tell what is running",
            "cannot tell which Claude Desktop processes are running, so killing "
            "and relaunching could leave two of them - refusing to act blind",
        )
    processes = list(report.processes)
    pids = [process.pid for process in processes]
    profiles = distinct_profile_dirs(processes)
    outcome = _partial_result(exe, pids, profiles, profile)
    if dry_run:
        # Run the gate even here. It only reads, so a dry run stays a dry run,
        # and it is the only way to watch the wait without restarting Desktop.
        # Report the exe the gate chose, or a dry run describes a launch that
        # differs from the one a real run would perform.
        dry_exe, dry_status = await_package_ready(gate)
        return _partial_result(dry_exe or exe, pids, profiles, profile)(
            launched=False, package_status=dry_status
        )
    if pids:
        effects.progress.publish(PHASE_STOPPING, "Stopping Claude Desktop")
        effects.killer(pids)
    if gate.enabled:
        # Unconditionally, even when nothing was killed: an empty snapshot can
        # also mean a Desktop that was mid-launch when we looked, and relaunching
        # over that is the same two-Desktops outcome.
        _settle(effects, waits, pids)
    elif pids:
        effects.sleeper(waits.settle_seconds)
    if no_launch:
        effects.progress.publish(PHASE_STOPPED, "Claude Desktop stopped")
        return outcome(launched=False)
    launched = _launch_verified(
        exe, launch_profile_dir(profile, profiles), waits, gate, effects, pids
    )
    return _partial_result(launched.exe, pids, profiles, profile)(
        launched=True,
        package_status=launched.package_status,
        attempts=launched.attempts,
    )


@dataclass(frozen=True)
class LaunchOutcome:
    exe: Path
    package_status: str | None
    attempts: int


# The unhardened path spawns and returns, exactly as it always did. The hardened
# one spawns, waits for Desktop to actually appear, and retries - but only when
# retrying is safe, which is the rule the loop below exists to enforce.
def _launch_verified(
    exe: Path,
    profile_dir: str | None,
    waits: Waits,
    gate: PackageGate,
    effects: Effects,
    killed: Sequence[int],
) -> LaunchOutcome:
    # One deadline for the whole restart, computed here and threaded into every
    # attempt. Recomputed per attempt it would multiply by the attempt count, and
    # a stuck package could hold the restart for the budget times eight.
    deadline = gate.clock() + gate.budget_seconds if gate.enabled else None
    attempts = waits.launch_attempts if gate.enabled else 1
    reason = ""
    for attempt in range(1, attempts + 1):
        # Resolve the exe per attempt, and only AFTER the kill: an update landing
        # mid-restart moves the package folder out from under a path resolved
        # earlier, which would fail every remaining attempt for a stale reason.
        gated_exe, package_status = await_package_ready(gate, deadline)
        target = gated_exe or exe
        if not gate.enabled:
            if not target.exists():
                # Published path-free, then raised in full: the caller needs the
                # path and the widget must never be handed one.
                effects.progress.publish(
                    PHASE_FAILED,
                    MISSING_PACKAGE,
                    error=MISSING_PACKAGE,
                )
                raise FileNotFoundError(f"Claude Desktop exe not found: {target}")
            effects.launcher(target, profile_dir)
            effects.progress.publish(PHASE_DONE, "Claude Desktop is running")
            return LaunchOutcome(target, package_status, attempt)
        verdict = _attempt(target, profile_dir, waits, effects, attempt)
        if verdict.up:
            effects.progress.publish(
                PHASE_DONE, "Claude Desktop is running", attempt=attempt
            )
            return LaunchOutcome(target, package_status, attempt)
        reason = verdict.reason
        # THE rule of this slice: a child that is still alive means Desktop is
        # coming up slowly, not failing. Spawning again there is how one restart
        # becomes two Desktops on two data dirs.
        if not verdict.retryable:
            _fail(effects, verdict.headline, verdict.reason, killed, attempt)
        if attempt < attempts:
            effects.sleeper(_backoff_seconds(waits, attempt))
    _fail(
        effects,
        "Claude Desktop would not start",
        f"Claude Desktop would not start after {attempts} attempts: {reason}",
        killed,
        attempts,
    )


# The exe going missing from under us mid-update is the very situation the
# hardened path exists to ride out, so it is a failed attempt rather than the end
# of the restart. Only the unhardened path, which has no second attempt to offer,
# still turns it into an error.
def _attempt(
    exe: Path, profile_dir: str | None, waits: Waits, effects: Effects, attempt: int
) -> UpVerdict:
    if not exe.exists():
        return UpVerdict(
            up=False,
            retryable=True,
            headline=MISSING_PACKAGE,
            reason=f"Claude Desktop exe not found: {exe}",
        )
    effects.progress.publish(
        PHASE_LAUNCHING, "Starting Claude Desktop", attempt=attempt
    )
    handle = effects.launcher(exe, profile_dir)
    effects.progress.publish(
        PHASE_WAITING_UP, "Waiting for Claude Desktop to appear", attempt=attempt
    )
    return _await_desktop_up(handle, waits, effects)


# The headline is what a person reads on the widget and the reason is what a
# caller reads in the error. They are separate because the reason is free to name
# a path and the headline never may - published strings stay path-free.
@dataclass(frozen=True)
class UpVerdict:
    up: bool
    retryable: bool = False
    reason: str = ""
    headline: str = "Claude Desktop did not come back"


DESKTOP_UP = UpVerdict(up=True)


# Whether Desktop appeared is decided by polling for IT, never by watching the
# process we spawned: an MSIX launcher hands off and exits within a second on a
# perfectly good launch, so reading that exit as failure would condemn a start
# that worked. The child's fate decides one thing only - whether another attempt
# is safe - and it deliberately does NOT cut the wait short. On MSIX the child
# always exits, so bailing out at the grace would make the grace the real
# deadline, and a Desktop merely slower than that would get a second one spawned
# on top of it.
def _await_desktop_up(
    handle: LaunchHandle | None, waits: Waits, effects: Effects
) -> UpVerdict:
    deadline = effects.clock() + waits.up_timeout_seconds
    exited_at: float | None = None
    while True:
        report = effects.finder()
        if not report.readable:
            # Blind. Another attempt would be a guess, and a wrong guess here
            # launches a second Desktop.
            return UpVerdict(
                up=False,
                headline="Lost sight of Claude Desktop",
                reason=(
                    "lost sight of Claude Desktop while waiting for it to come "
                    "back - cannot tell whether it is up"
                ),
            )
        if report.processes:
            return DESKTOP_UP
        exited_at = _exit_time(handle, exited_at, effects)
        if effects.clock() >= deadline:
            break
        effects.sleeper(waits.up_poll_seconds)
    # Retrying is safe only if the child is long gone. A child that exited in the
    # last instant before the deadline has not yet earned that, and one still
    # running never does.
    if _grace_spent(exited_at, waits, effects):
        return _exited_verdict()
    return UpVerdict(
        up=False,
        headline="Claude Desktop is not responding",
        reason="gave up waiting for Claude Desktop: it did not appear in time",
    )


def _backoff_seconds(waits: Waits, attempt: int) -> float:
    return min(waits.launch_backoff_seconds * attempt, waits.launch_backoff_cap_seconds)


def _exit_time(
    handle: LaunchHandle | None, exited_at: float | None, effects: Effects
) -> float | None:
    if exited_at is not None:
        return exited_at
    return None if _child_alive(handle) else effects.clock()


def _grace_spent(exited_at: float | None, waits: Waits, effects: Effects) -> bool:
    if exited_at is None:
        return False
    return effects.clock() - exited_at >= waits.exit_grace_seconds


def _exited_verdict() -> UpVerdict:
    return UpVerdict(
        up=False,
        retryable=True,
        headline="Claude Desktop did not start",
        reason="the launched process exited without starting Claude Desktop",
    )


# Publishing the failure and raising it are one act: every path that gives up
# must leave the same evidence behind, and one that only raises leaves a widget
# showing the last thing that went right.
def _fail(
    effects: Effects,
    headline: str,
    reason: str,
    killed: Sequence[int] = (),
    attempts: int = 0,
) -> NoReturn:
    effects.progress.publish(
        PHASE_FAILED, headline, attempt=attempts, error=headline
    )
    raise RestartBlocked(reason, killed, attempts)


# A launcher that reports nothing leaves the child's fate unknown, and unknown
# has to read as "still alive": the retry the other answer unlocks is precisely
# the one that produces a second Desktop.
def _child_alive(handle: LaunchHandle | None) -> bool:
    if handle is None:
        return True
    try:
        return bool(handle.running())
    except Exception:
        return True


# taskkill returning is not termination, and the pid list was a snapshot taken
# before the kill, so a process that appears during it is invisible to it. The
# blind sleep is REPLACED here rather than kept alongside: waiting twice would be
# a second of latency plus the same hazard.
def _settle(effects: Effects, waits: Waits, killed: Sequence[int]) -> None:
    effects.progress.publish(PHASE_WAITING_DOWN, "Waiting for Claude Desktop to exit")
    deadline = effects.clock() + waits.down_timeout_seconds
    while True:
        report = effects.finder()
        if not report.readable:
            _fail(
                effects,
                "Lost sight of Claude Desktop",
                "lost sight of Claude Desktop while waiting for it to exit - "
                "cannot tell whether it is down",
                killed,
            )
        if not report.processes:
            return
        if effects.clock() >= deadline:
            _fail(
                effects,
                "Claude Desktop would not stop",
                "Claude Desktop was still running "
                f"{waits.down_timeout_seconds:g}s after being killed",
                killed,
            )
        effects.sleeper(waits.down_poll_seconds)


def await_package_ready(
    gate: PackageGate, deadline: float | None = None
) -> tuple[Path | None, str | None]:
    if not gate.enabled:
        return None, None
    if deadline is None:
        deadline = gate.clock() + gate.budget_seconds
    while gate.clock() < deadline:
        report = _read_or_unreadable(gate)
        if not report.readable:
            # Waiting out the budget on a reader that cannot answer just delays
            # the same outcome. Let the launch be judged on its own result.
            return _gate_result(gate, None, PACKAGE_STATUS_UNREADABLE)
        ready = ready_package(report.packages)
        if ready is not None:
            return _gate_result(gate, ready.exe, PACKAGE_STATUS_OK)
        gate.on_status(package_status_summary(report.packages))
        gate.sleeper(gate.poll_seconds)
    return _gate_result(gate, None, PACKAGE_STATUS_BUDGET_SPENT)


# By the time the gate runs, Desktop has already been killed. A reader that
# raises - no powershell on PATH, a locked-down shell - must not escape and take
# the relaunch with it, because the caller would see an error while Desktop
# stays down. Any failure to read is simply "unreadable", which fails open.
def _read_or_unreadable(gate: PackageGate) -> PackageReport:
    try:
        return gate.reader()
    except Exception:
        return PackageReport(readable=False)


def _gate_result(
    gate: PackageGate, exe: Path | None, status: str
) -> tuple[Path | None, str]:
    gate.on_status(status)
    return exe, status


def launch_profile_dir(profile: ProfileChoice, observed: Sequence[str]) -> str | None:
    if profile.bare:
        return None
    if profile.explicit:
        return profile.explicit
    return observed[0] if observed else None


def profile_source(profile: ProfileChoice) -> str:
    if profile.bare:
        return PROFILE_SOURCE_NONE
    if profile.explicit:
        return PROFILE_SOURCE_EXPLICIT
    return PROFILE_SOURCE_INFERRED


def _partial_result(
    exe: Path, pids: list[int], profiles: list[str], profile: ProfileChoice
) -> Callable[..., Result]:
    def build(
        *, launched: bool, package_status: str | None = None, attempts: int = 0
    ) -> Result:
        return Result(
            killed=pids,
            launched=launched,
            exe=exe,
            profile_dir=profiles[0] if profiles else None,
            profile_conflict=len(profiles) > 1,
            launch_profile_dir=launch_profile_dir(profile, profiles),
            profile_source=profile_source(profile),
            package_status=package_status,
            attempts=attempts,
        )

    return build


def _powershell(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-Command", script]


def _default_capture(cmd: Sequence[str]) -> CompletedLike:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
    return CompletedLike(stdout=proc.stdout, returncode=proc.returncode)


def _default_launch(exe: Path, profile_dir: str | None = None) -> LaunchHandle:
    argv = [str(exe)]
    if profile_dir:
        argv.append(f"{PROFILE_FLAG}={profile_dir}")
    child = subprocess.Popen(
        argv,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return LaunchHandle(running=lambda: child.poll() is None)
