from __future__ import annotations

from pathlib import Path

import pytest

from hard_restart_claude_code import restart as restart_mod
from hard_restart_claude_code.restart import CompletedLike, Result, find_pids, hard_restart


def test_find_pids_parses_powershell_output():
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return CompletedLike(stdout="1234\n5678\n\n  9012  \n")

    assert find_pids(runner) == [1234, 5678, 9012]
    assert captured["cmd"][0] == "powershell"
    assert "Get-Process claude" in " ".join(captured["cmd"])


def test_find_pids_ignores_non_numeric_lines():
    runner = lambda _cmd: CompletedLike(stdout="header\n42\nfoo\n")
    assert find_pids(runner) == [42]


def test_hard_restart_kills_then_launches(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")  # exists
    events = []

    result = hard_restart(
        exe,
        finder=lambda: [10, 11],
        killer=lambda pids: events.append(("kill", pids)),
        launcher=lambda e: events.append(("launch", e)),
        sleeper=lambda _s: events.append(("sleep",)),
    )
    assert result == Result(killed=[10, 11], launched=True, exe=exe)
    assert events == [("kill", [10, 11]), ("sleep",), ("launch", exe)]


def test_hard_restart_skips_kill_when_no_pids(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    events = []

    hard_restart(
        exe,
        finder=lambda: [],
        killer=lambda pids: events.append(("kill", pids)),
        launcher=lambda e: events.append(("launch", e)),
        sleeper=lambda _s: events.append(("sleep",)),
    )
    assert events == [("launch", exe)]


def test_hard_restart_dry_run_does_nothing(tmp_path):
    exe = tmp_path / "claude.exe"
    events = []

    result = hard_restart(
        exe,
        dry_run=True,
        finder=lambda: [99],
        killer=lambda pids: events.append("kill"),
        launcher=lambda e: events.append("launch"),
        sleeper=lambda _s: events.append("sleep"),
    )
    assert result.killed == [99]
    assert result.launched is False
    assert events == []


def test_hard_restart_no_launch_kills_only(tmp_path):
    exe = tmp_path / "claude.exe"
    events = []

    result = hard_restart(
        exe,
        no_launch=True,
        finder=lambda: [7],
        killer=lambda pids: events.append(("kill", pids)),
        launcher=lambda e: events.append(("launch", e)),
        sleeper=lambda _s: None,
    )
    assert result == Result(killed=[7], launched=False, exe=exe)
    assert events == [("kill", [7])]


def test_hard_restart_missing_exe_raises(tmp_path):
    missing = tmp_path / "nope.exe"
    with pytest.raises(FileNotFoundError):
        hard_restart(
            missing,
            finder=lambda: [],
            killer=lambda pids: None,
            launcher=lambda e: None,
            sleeper=lambda _s: None,
        )


def test_default_exe_path_is_d_drive():
    assert str(restart_mod.DEFAULT_EXE).startswith("D:\\WindowsApps\\Claude_")
