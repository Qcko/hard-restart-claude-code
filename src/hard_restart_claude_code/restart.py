from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROCESS_BASENAME = "claude"
INSTALL_HINT = r"WindowsApps\Claude_"
APPX_PACKAGE_NAME = "Claude"
EXE_RELATIVE = Path("app") / "claude.exe"

Runner = Callable[[Sequence[str]], "CompletedLike"]


@dataclass(frozen=True)
class CompletedLike:
    stdout: str = ""
    returncode: int = 0


@dataclass(frozen=True)
class Result:
    killed: list[int]
    launched: bool
    exe: Path


def discover_exe(runner: Runner | None = None) -> Path | None:
    runner = runner or _default_capture
    install_location = _query_install_location(runner)
    if not install_location:
        return None
    exe = install_location / EXE_RELATIVE
    return exe if exe.is_file() else None


def _query_install_location(runner: Runner) -> Path | None:
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        f"Get-AppxPackage -Name '{APPX_PACKAGE_NAME}' -ErrorAction SilentlyContinue | "
        f"Select-Object -ExpandProperty InstallLocation",
    ]
    proc = runner(cmd)
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return Path(stripped)
    return None


def find_pids(runner: Runner | None = None) -> list[int]:
    runner = runner or _default_capture
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        f"Get-Process {PROCESS_BASENAME} -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Path -like '*{INSTALL_HINT}*' }} | "
        f"Select-Object -ExpandProperty Id",
    ]
    proc = runner(cmd)
    return [int(line) for line in proc.stdout.splitlines() if line.strip().isdigit()]


def kill_pids(pids: list[int], runner: Runner | None = None) -> None:
    runner = runner or _default_capture
    for pid in pids:
        runner(["taskkill", "/F", "/PID", str(pid)])


def launch(exe: Path, launcher: Callable[[Path], None] | None = None) -> None:
    launcher = launcher or _default_launch
    launcher(exe)


def hard_restart(
    exe: Path,
    *,
    dry_run: bool = False,
    no_launch: bool = False,
    settle_seconds: float = 1.0,
    finder: Callable[[], list[int]] = find_pids,
    killer: Callable[[list[int]], None] = kill_pids,
    launcher: Callable[[Path], None] = launch,
    sleeper: Callable[[float], None] = time.sleep,
) -> Result:
    pids = finder()
    if dry_run:
        return Result(killed=pids, launched=False, exe=exe)
    if pids:
        killer(pids)
        sleeper(settle_seconds)
    if no_launch:
        return Result(killed=pids, launched=False, exe=exe)
    if not exe.exists():
        raise FileNotFoundError(f"Claude Desktop exe not found: {exe}")
    launcher(exe)
    return Result(killed=pids, launched=True, exe=exe)


def _default_capture(cmd: Sequence[str]) -> CompletedLike:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
    return CompletedLike(stdout=proc.stdout, returncode=proc.returncode)


def _default_launch(exe: Path) -> None:
    subprocess.Popen(
        [str(exe)],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
