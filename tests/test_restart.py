from __future__ import annotations

import json
from pathlib import Path

import pytest

from hard_restart_claude_code.restart import (
    ClaudeProcess,
    CompletedLike,
    Result,
    discover_exe,
    distinct_profile_dirs,
    find_processes,
    hard_restart,
    kill_pids,
    parse_profile_dir,
    selected_profile_dir,
)

DESKTOP_PATH = (
    r"C:\Program Files\WindowsApps"
    r"\Claude_1.32352.1.0_x64__pzs8sxrjxfjjc\app\claude.exe"
)
RESERVE_CLI_PATH = (
    r"E:\dev\secrets\account-swap\userdata\reserve\claude-code\2.1.229\claude.exe"
)


def _rows(*rows) -> str:
    return json.dumps(list(rows))


def _desktop_row(pid: int, command_line: str = "") -> dict:
    return {
        "ProcessId": pid,
        "ExecutablePath": DESKTOP_PATH,
        "CommandLine": command_line or f'"{DESKTOP_PATH}"',
    }


def test_find_processes_parses_json_rows():
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return CompletedLike(stdout=_rows(_desktop_row(1234), _desktop_row(5678)))

    processes = find_processes(runner)
    assert [p.pid for p in processes] == [1234, 5678]
    assert captured["cmd"][0] == "powershell"
    joined = " ".join(captured["cmd"])
    assert "Win32_Process" in joined
    assert "ExecutablePath" in joined


def test_find_processes_returns_empty_for_empty_array():
    runner = lambda _cmd: CompletedLike(stdout="[]")
    assert find_processes(runner) == []


def test_find_processes_returns_empty_for_blank_output():
    runner = lambda _cmd: CompletedLike(stdout="   \n")
    assert find_processes(runner) == []


def test_find_processes_accepts_single_object_not_array():
    runner = lambda _cmd: CompletedLike(stdout=json.dumps(_desktop_row(99)))
    assert [p.pid for p in find_processes(runner)] == [99]


def test_find_processes_ignores_rows_without_integer_pid():
    runner = lambda _cmd: CompletedLike(
        stdout=_rows({"ProcessId": None, "ExecutablePath": DESKTOP_PATH}, _desktop_row(7))
    )
    assert [p.pid for p in find_processes(runner)] == [7]


def test_find_processes_survives_non_json_output():
    runner = lambda _cmd: CompletedLike(stdout="Get-CimInstance : access denied")
    assert find_processes(runner) == []


def test_query_filters_to_windowsapps_install_path():
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return CompletedLike(stdout="[]")

    find_processes(runner)
    assert r"WindowsApps\Claude_" in " ".join(captured["cmd"])


def test_reserve_account_swap_cli_is_never_matched():
    # The account-swap managed CLI shares the claude.exe basename. It must stay
    # outside the match, and it is the PowerShell path filter that excludes it.
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return CompletedLike(stdout="[]")

    find_processes(runner)
    query = " ".join(captured["cmd"])
    assert RESERVE_CLI_PATH.lower() not in query.lower()
    assert "*Claude*" not in query
    assert r"*WindowsApps\Claude_*" in query


def test_parse_profile_dir_handles_equals_form():
    assert parse_profile_dir(r'claude.exe --user-data-dir=C:\p\reserve') == r"C:\p\reserve"


def test_parse_profile_dir_handles_space_form():
    assert parse_profile_dir(r'claude.exe --user-data-dir C:\p\reserve') == r"C:\p\reserve"


def test_parse_profile_dir_handles_quoted_path_with_spaces():
    line = 'claude.exe --user-data-dir="C:\\my profile\\reserve"'
    assert parse_profile_dir(line) == r"C:\my profile\reserve"


def test_parse_profile_dir_returns_none_when_absent():
    assert parse_profile_dir(r'"C:\...\claude.exe"') is None


def test_parse_profile_dir_returns_none_for_empty_command_line():
    assert parse_profile_dir("") is None


def test_find_processes_extracts_profile_from_command_line():
    row = _desktop_row(11, f'"{DESKTOP_PATH}" --user-data-dir=D:\\profiles\\reserve')
    runner = lambda _cmd: CompletedLike(stdout=_rows(row))
    assert find_processes(runner)[0].profile_dir == r"D:\profiles\reserve"


def test_selected_profile_dir_takes_the_first_profile_in_order():
    processes = [
        ClaudeProcess(pid=1),
        ClaudeProcess(pid=2, profile_dir=r"D:\reserve"),
        ClaudeProcess(pid=3, profile_dir=r"D:\other"),
    ]
    assert selected_profile_dir(processes) == r"D:\reserve"


def test_selected_profile_dir_is_none_when_all_bare():
    assert selected_profile_dir([ClaudeProcess(pid=1), ClaudeProcess(pid=2)]) is None


def test_kill_pids_does_not_tree_kill():
    calls = []
    kill_pids([42], runner=lambda cmd: calls.append(cmd) or CompletedLike())
    assert calls == [["taskkill", "/F", "/PID", "42"]]


def test_hard_restart_kills_then_launches_with_profile(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    events = []

    result = hard_restart(
        exe,
        finder=lambda: [
            ClaudeProcess(pid=10),
            ClaudeProcess(pid=11, profile_dir=r"D:\reserve"),
        ],
        killer=lambda pids: events.append(("kill", pids)),
        launcher=lambda e, profile: events.append(("launch", e, profile)),
        sleeper=lambda _s: events.append(("sleep",)),
    )
    assert result == Result(
        killed=[10, 11], launched=True, exe=exe, profile_dir=r"D:\reserve"
    )
    assert events == [
        ("kill", [10, 11]),
        ("sleep",),
        ("launch", exe, r"D:\reserve"),
    ]


def test_hard_restart_launches_bare_when_no_profile_in_use(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    events = []

    hard_restart(
        exe,
        finder=lambda: [ClaudeProcess(pid=10)],
        killer=lambda _pids: None,
        launcher=lambda e, profile: events.append(("launch", e, profile)),
        sleeper=lambda _s: None,
    )
    assert events == [("launch", exe, None)]


def test_hard_restart_skips_kill_when_no_pids(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    events = []

    hard_restart(
        exe,
        finder=lambda: [],
        killer=lambda pids: events.append(("kill", pids)),
        launcher=lambda e, profile: events.append(("launch", e, profile)),
        sleeper=lambda _s: events.append(("sleep",)),
    )
    assert events == [("launch", exe, None)]


def test_hard_restart_dry_run_reports_profile_without_acting(tmp_path):
    exe = tmp_path / "claude.exe"
    events = []

    result = hard_restart(
        exe,
        dry_run=True,
        finder=lambda: [ClaudeProcess(pid=99, profile_dir=r"D:\reserve")],
        killer=lambda _pids: events.append("kill"),
        launcher=lambda _e, _p: events.append("launch"),
        sleeper=lambda _s: events.append("sleep"),
    )
    assert result.killed == [99]
    assert result.launched is False
    assert result.profile_dir == r"D:\reserve"
    assert events == []


def test_hard_restart_no_launch_kills_only(tmp_path):
    exe = tmp_path / "claude.exe"
    events = []

    result = hard_restart(
        exe,
        no_launch=True,
        finder=lambda: [ClaudeProcess(pid=7, profile_dir=r"D:\reserve")],
        killer=lambda pids: events.append(("kill", pids)),
        launcher=lambda _e, _p: events.append("launch"),
        sleeper=lambda _s: None,
    )
    assert result == Result(
        killed=[7], launched=False, exe=exe, profile_dir=r"D:\reserve"
    )
    assert events == [("kill", [7])]


def test_hard_restart_missing_exe_raises(tmp_path):
    missing = tmp_path / "nope.exe"
    with pytest.raises(FileNotFoundError):
        hard_restart(
            missing,
            finder=lambda: [],
            killer=lambda _pids: None,
            launcher=lambda _e, _p: None,
            sleeper=lambda _s: None,
        )


def _make_install(root: Path) -> Path:
    app = root / "app"
    app.mkdir(parents=True)
    exe = app / "claude.exe"
    exe.write_text("")
    return exe


def _location_rows(*locations: str) -> str:
    return json.dumps([{"InstallLocation": loc} for loc in locations])


def test_discover_exe_returns_path_under_install_location(tmp_path):
    exe = _make_install(tmp_path)
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return CompletedLike(stdout=_location_rows(str(tmp_path)))

    assert discover_exe(runner) == exe
    joined = " ".join(captured["cmd"])
    assert "Get-AppxPackage" in joined
    assert "-Name 'Claude'" in joined


def test_discover_exe_returns_none_when_package_missing(tmp_path):
    runner = lambda _cmd: CompletedLike(stdout="[]")
    assert discover_exe(runner) is None


def test_discover_exe_returns_none_when_exe_does_not_exist(tmp_path):
    runner = lambda _cmd: CompletedLike(stdout=_location_rows(str(tmp_path)))
    assert discover_exe(runner) is None


def test_discover_exe_returns_none_when_two_packages_are_staged(tmp_path):
    # Mid-deployment Get-AppxPackage can list the staged and installed packages
    # together. Picking either one is a coin flip, so refuse instead.
    other = tmp_path / "other"
    _make_install(tmp_path)
    runner = lambda _cmd: CompletedLike(
        stdout=_location_rows(str(tmp_path), str(other))
    )
    assert discover_exe(runner) is None


def test_discover_exe_tolerates_duplicate_identical_locations(tmp_path):
    exe = _make_install(tmp_path)
    runner = lambda _cmd: CompletedLike(
        stdout=_location_rows(str(tmp_path), str(tmp_path))
    )
    assert discover_exe(runner) == exe


def test_find_processes_are_sorted_by_pid():
    runner = lambda _cmd: CompletedLike(
        stdout=_rows(_desktop_row(900), _desktop_row(12), _desktop_row(400))
    )
    assert [p.pid for p in find_processes(runner)] == [12, 400, 900]


def test_query_filters_on_executable_path_not_command_line():
    captured = {}

    def runner(cmd):
        captured["cmd"] = cmd
        return CompletedLike(stdout="[]")

    find_processes(runner)
    assert "$_.ExecutablePath -like" in " ".join(captured["cmd"])


def test_distinct_profile_dirs_dedupes_and_keeps_order():
    processes = [
        ClaudeProcess(pid=1, profile_dir=r"D:\reserve"),
        ClaudeProcess(pid=2),
        ClaudeProcess(pid=3, profile_dir=r"D:\reserve"),
        ClaudeProcess(pid=4, profile_dir=r"D:\other"),
    ]
    assert distinct_profile_dirs(processes) == [r"D:\reserve", r"D:\other"]


def test_selected_profile_dir_is_lowest_pid_when_sorted():
    processes = [
        ClaudeProcess(pid=2, profile_dir=r"D:\reserve"),
        ClaudeProcess(pid=3, profile_dir=r"D:\other"),
    ]
    assert selected_profile_dir(processes) == r"D:\reserve"


def test_hard_restart_flags_conflict_when_two_profiles_run(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    launched = []

    result = hard_restart(
        exe,
        finder=lambda: [
            ClaudeProcess(pid=1, profile_dir=r"D:\reserve"),
            ClaudeProcess(pid=2, profile_dir=r"D:\other"),
        ],
        killer=lambda _pids: None,
        launcher=lambda e, profile: launched.append(profile),
        sleeper=lambda _s: None,
    )
    assert result.profile_conflict is True
    assert result.profile_dir == r"D:\reserve"
    assert launched == [r"D:\reserve"]


def test_hard_restart_reports_no_conflict_for_single_profile(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")

    result = hard_restart(
        exe,
        finder=lambda: [
            ClaudeProcess(pid=1, profile_dir=r"D:\reserve"),
            ClaudeProcess(pid=2, profile_dir=r"D:\reserve"),
        ],
        killer=lambda _pids: None,
        launcher=lambda _e, _p: None,
        sleeper=lambda _s: None,
    )
    assert result.profile_conflict is False


def test_profile_is_found_on_a_child_when_main_process_lacks_the_flag():
    # Some Desktop builds drop --user-data-dir from the main process command
    # line while Chromium still requires it on every sandboxed child, so the
    # profile must be taken from whichever process carries one.
    main = _desktop_row(100, f'"{DESKTOP_PATH}"')
    renderer = _desktop_row(
        200, rf'"{DESKTOP_PATH}" --type=renderer --user-data-dir=D:\profiles\reserve'
    )
    runner = lambda _cmd: CompletedLike(stdout=_rows(main, renderer))
    processes = find_processes(runner)
    assert processes[0].profile_dir is None
    assert selected_profile_dir(processes) == r"D:\profiles\reserve"
