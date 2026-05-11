from __future__ import annotations

from pathlib import Path

import pytest

from hard_restart_claude_code.restart import (
    CompletedLike,
    Result,
    discover_exe,
    find_pids,
    hard_restart,
)


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
    exe.write_text("")
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


def _make_install(root: Path) -> Path:
    app = root / "app"
    app.mkdir(parents=True)
    exe = app / "claude.exe"
    exe.write_text("")
    return exe


def test_discover_exe_returns_path_under_install_location(tmp_path):
    exe = _make_install(tmp_path)
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return CompletedLike(stdout=f"{tmp_path}\n")

    assert discover_exe(runner) == exe
    assert "Get-AppxPackage" in " ".join(captured["cmd"])
    assert "-Name 'Claude'" in " ".join(captured["cmd"])


def test_discover_exe_returns_none_when_package_missing(tmp_path):
    runner = lambda _cmd: CompletedLike(stdout="")
    assert discover_exe(runner) is None


def test_discover_exe_returns_none_when_exe_does_not_exist(tmp_path):
    # InstallLocation exists but app/claude.exe inside does not.
    runner = lambda _cmd: CompletedLike(stdout=f"{tmp_path}\n")
    assert discover_exe(runner) is None


def test_discover_exe_strips_whitespace(tmp_path):
    exe = _make_install(tmp_path)
    runner = lambda _cmd: CompletedLike(stdout=f"  {tmp_path}  \n\n")
    assert discover_exe(runner) == exe
