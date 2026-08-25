from __future__ import annotations

import json
import os
import re
import subprocess
import time
import winreg
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROCESS_IMAGE_NAME = "claude.exe"
APPX_PACKAGE_NAME = "Claude"
PACKAGE_DIR_PREFIX = "Claude_"
WINDOWSAPPS_DIR = "WindowsApps"
DEFAULT_PROGRAM_FILES = r"C:\Program Files"
CURRENT_VERSION_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
PROGRAM_FILES_VALUE = "ProgramFilesDir"
EXE_RELATIVE = Path("app") / "claude.exe"
PROFILE_FLAG = "--user-data-dir"

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
class Result:
    killed: list[int]
    launched: bool
    exe: Path
    profile_dir: str | None = None
    profile_conflict: bool = False
    launch_profile_dir: str | None = None
    profile_source: str = PROFILE_SOURCE_INFERRED


def discover_exe(runner: Runner | None = None) -> Path | None:
    runner = runner or _default_capture
    install_location = _query_install_location(runner)
    if not install_location:
        return None
    exe = install_location / EXE_RELATIVE
    return exe if exe.is_file() else None


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


def find_processes(
    runner: Runner | None = None, install_prefix: str | None = None
) -> list[ClaudeProcess]:
    runner = runner or _default_capture
    cmd = _powershell(
        f"ConvertTo-Json -Depth 3 -InputObject @(Get-CimInstance Win32_Process "
        f"-Filter \"Name='{PROCESS_IMAGE_NAME}'\" -ErrorAction SilentlyContinue | "
        f"Select-Object ProcessId,ExecutablePath,CommandLine)"
    )
    processes = _decode_processes(runner(cmd).stdout)
    return [
        process
        for process in processes
        if is_package_install_path(process.path, install_prefix)
    ]


def _decode_processes(stdout: str) -> list[ClaudeProcess]:
    processes = []
    for row in _decode_rows(stdout):
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


def _decode_rows(stdout: str) -> list[dict]:
    text = stdout.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except ValueError:
        return []
    if isinstance(decoded, dict):
        return [decoded]
    if not isinstance(decoded, list):
        return []
    return [row for row in decoded if isinstance(row, dict)]


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


def kill_pids(pids: list[int], runner: Runner | None = None) -> None:
    runner = runner or _default_capture
    for pid in pids:
        runner(["taskkill", "/F", "/PID", str(pid)])


def launch(
    exe: Path,
    profile_dir: str | None = None,
    launcher: Callable[[Path, str | None], None] | None = None,
) -> None:
    launcher = launcher or _default_launch
    launcher(exe, profile_dir)


def hard_restart(
    exe: Path,
    *,
    dry_run: bool = False,
    no_launch: bool = False,
    settle_seconds: float = 1.0,
    profile: ProfileChoice = INFERRED,
    finder: Callable[[], list[ClaudeProcess]] = find_processes,
    killer: Callable[[list[int]], None] = kill_pids,
    launcher: Callable[[Path, str | None], None] = launch,
    sleeper: Callable[[float], None] = time.sleep,
) -> Result:
    processes = finder()
    pids = [process.pid for process in processes]
    profiles = distinct_profile_dirs(processes)
    outcome = _partial_result(exe, pids, profiles, profile)
    if dry_run:
        return outcome(launched=False)
    if pids:
        killer(pids)
        sleeper(settle_seconds)
    if no_launch:
        return outcome(launched=False)
    if not exe.exists():
        raise FileNotFoundError(f"Claude Desktop exe not found: {exe}")
    launcher(exe, launch_profile_dir(profile, profiles))
    return outcome(launched=True)


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
    def build(*, launched: bool) -> Result:
        return Result(
            killed=pids,
            launched=launched,
            exe=exe,
            profile_dir=profiles[0] if profiles else None,
            profile_conflict=len(profiles) > 1,
            launch_profile_dir=launch_profile_dir(profile, profiles),
            profile_source=profile_source(profile),
        )

    return build


def _powershell(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-Command", script]


def _default_capture(cmd: Sequence[str]) -> CompletedLike:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
    return CompletedLike(stdout=proc.stdout, returncode=proc.returncode)


def _default_launch(exe: Path, profile_dir: str | None = None) -> None:
    argv = [str(exe)]
    if profile_dir:
        argv.append(f"{PROFILE_FLAG}={profile_dir}")
    subprocess.Popen(
        argv,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
