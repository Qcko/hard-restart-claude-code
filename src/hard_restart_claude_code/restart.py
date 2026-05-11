from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXE = Path(r"D:\WindowsApps\Claude_1.6608.2.0_x64__pzs8sxrjxfjjc\app\claude.exe")
PROCESS_BASENAME = "claude"
INSTALL_HINT = r"WindowsApps\Claude_"

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
    exe: Path = DEFAULT_EXE,
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
