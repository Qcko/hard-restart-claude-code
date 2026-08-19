from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROCESS_IMAGE_NAME = "claude.exe"
INSTALL_HINT = r"WindowsApps\Claude_"
APPX_PACKAGE_NAME = "Claude"
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


@dataclass(frozen=True)
class Result:
    killed: list[int]
    launched: bool
    exe: Path
    profile_dir: str | None = None
    profile_conflict: bool = False


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


def find_processes(runner: Runner | None = None) -> list[ClaudeProcess]:
    runner = runner or _default_capture
    cmd = _powershell(
        f"ConvertTo-Json -Depth 3 -InputObject @(Get-CimInstance Win32_Process "
        f"-Filter \"Name='{PROCESS_IMAGE_NAME}'\" -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.ExecutablePath -like '*{INSTALL_HINT}*' }} | "
        f"Select-Object ProcessId,ExecutablePath,CommandLine)"
    )
    return _decode_processes(runner(cmd).stdout)


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
    finder: Callable[[], list[ClaudeProcess]] = find_processes,
    killer: Callable[[list[int]], None] = kill_pids,
    launcher: Callable[[Path, str | None], None] = launch,
    sleeper: Callable[[float], None] = time.sleep,
) -> Result:
    processes = finder()
    pids = [process.pid for process in processes]
    profiles = distinct_profile_dirs(processes)
    outcome = _partial_result(exe, pids, profiles)
    if dry_run:
        return outcome(launched=False)
    if pids:
        killer(pids)
        sleeper(settle_seconds)
    if no_launch:
        return outcome(launched=False)
    if not exe.exists():
        raise FileNotFoundError(f"Claude Desktop exe not found: {exe}")
    launcher(exe, profiles[0] if profiles else None)
    return outcome(launched=True)


def _partial_result(
    exe: Path, pids: list[int], profiles: list[str]
) -> Callable[..., Result]:
    def build(*, launched: bool) -> Result:
        return Result(
            killed=pids,
            launched=launched,
            exe=exe,
            profile_dir=profiles[0] if profiles else None,
            profile_conflict=len(profiles) > 1,
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
