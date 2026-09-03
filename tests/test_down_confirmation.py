from __future__ import annotations

import pytest

from hard_restart_claude_code.cli import EXIT_CANNOT_CONFIRM, main
from hard_restart_claude_code.restart import (
    ClaudeProcess,
    CompletedLike,
    Effects,
    PackageGate,
    ProcessReport,
    RestartBlocked,
    Waits,
    hard_restart,
    survey_processes,
)

VERIFYING = PackageGate(enabled=True, budget_seconds=0.0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Desktop:
    """Reports processes until it is told how many polls it takes to die.

    It comes back the moment it is launched, because the hardened path now waits
    for that: a fake that stays dead after a launch would hold the up-wait open
    for its whole timeout instead of testing the down-confirmation.
    """

    def __init__(self, dies_after: int | None = 0, readable: bool = True) -> None:
        self.dies_after = dies_after
        self.readable = readable
        self.polls = 0
        self.killed: list[int] = []
        self.relaunched = False

    def find(self) -> ProcessReport:
        if not self.readable:
            return ProcessReport(readable=False)
        gone = (
            self.killed
            and not self.relaunched
            and (self.dies_after is not None and self.polls >= self.dies_after)
        )
        self.polls += 1
        return ProcessReport(True, () if gone else (ClaudeProcess(pid=1),))

    def kill(self, pids) -> None:
        self.killed.extend(pids)

    def launch(self) -> None:
        self.relaunched = True


def _run(desktop: Desktop, exe, *, hardened=True, launched=None, waits=Waits()):
    clock = FakeClock()
    return hard_restart(
        exe,
        waits=waits,
        gate=VERIFYING if hardened else PackageGate(),
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=lambda e, _dir: _record(desktop, launched, e),
            sleeper=clock.sleep,
            clock=clock,
        ),
    )


def _record(desktop: Desktop, launched, exe) -> None:
    (launched if launched is not None else []).append(exe)
    desktop.launch()


def _exe(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    return exe


def test_launch_waits_until_the_processes_are_actually_gone(tmp_path):
    desktop = Desktop(dies_after=3)
    launched: list = []
    _run(desktop, _exe(tmp_path), launched=launched)
    assert desktop.killed == [1]
    assert launched  # it did eventually launch
    # One enumeration, three down-confirmation polls, one up-confirmation poll.
    # Pinned exactly: the up-wait shares this counter, so a loose bound would
    # also pass with a down-confirmation that stopped a poll short.
    assert desktop.polls == 5


def test_a_desktop_that_will_not_die_blocks_the_relaunch(tmp_path):
    desktop = Desktop(dies_after=None)
    launched: list = []
    with pytest.raises(RestartBlocked, match="still running"):
        _run(desktop, _exe(tmp_path), launched=launched)
    assert launched == []


def test_losing_sight_of_desktop_mid_wait_blocks_the_relaunch(tmp_path):
    desktop = Desktop(dies_after=None)
    launched: list = []

    def find_then_go_blind() -> ProcessReport:
        if desktop.killed:
            return ProcessReport(readable=False)
        return ProcessReport(True, (ClaudeProcess(pid=1),))

    desktop.find = find_then_go_blind
    with pytest.raises(RestartBlocked, match="lost sight"):
        _run(desktop, _exe(tmp_path), launched=launched)
    assert launched == []


def test_an_unreadable_survey_blocks_before_anything_is_killed(tmp_path):
    desktop = Desktop(readable=False)
    launched: list = []
    with pytest.raises(RestartBlocked, match="refusing to act blind"):
        _run(desktop, _exe(tmp_path), launched=launched)
    assert desktop.killed == []
    assert launched == []


# A bare hrcc keeps the blind settle. It is the fast path, and it is what a human
# typing `hrcc` has always got.
def test_the_unhardened_path_still_just_sleeps(tmp_path):
    desktop = Desktop(dies_after=None)
    slept: list[float] = []
    launched: list = []
    result = hard_restart(
        _exe(tmp_path),
        waits=Waits(settle_seconds=1.5),
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=lambda e, _d: launched.append(e),
            sleeper=lambda s: slept.append(s),
        ),
    )
    assert result.launched is True
    assert launched
    assert slept == [1.5]  # the old blind settle, unchanged
    assert desktop.polls == 1  # enumerated once, never polled again


def test_hardened_no_launch_still_confirms_the_kill(tmp_path):
    desktop = Desktop(dies_after=1)
    launched: list = []
    result = hard_restart(
        _exe(tmp_path),
        no_launch=True,
        gate=VERIFYING,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=lambda e, _d: _record(desktop, launched, e),
            sleeper=lambda _s: None,
            clock=FakeClock(),
        ),
    )
    assert desktop.killed == [1]
    assert result.launched is False
    assert launched == []


def test_a_blocked_restart_reports_what_it_already_killed(tmp_path, capsys, monkeypatch):
    import hard_restart_claude_code.cli as cli_module
    import json as json_module

    def blocked(*_args, **_kwargs):
        raise RestartBlocked("Desktop was still running", [11, 22])

    monkeypatch.setattr(cli_module, "hard_restart", blocked)
    code = main(["--exe", str(_exe(tmp_path)), "--verify", "--json"])
    payload = json_module.loads(capsys.readouterr().out)
    assert code == EXIT_CANNOT_CONFIRM
    # Desktop is down and did not come back - a caller must be able to tell that
    # from "nothing happened, safe to retry".
    assert payload["killed"] == [11, 22]


def test_the_hardened_path_does_not_also_sleep_the_settle(tmp_path):
    # dies_after=2 so the loop really polls and really sleeps - with an
    # immediate death this would pass even if the settle sleep were still there.
    slept: list[float] = []
    desktop = Desktop(dies_after=2)
    clock = FakeClock()
    waits = Waits(settle_seconds=99.0, down_poll_seconds=0.25)
    hard_restart(
        _exe(tmp_path),
        waits=waits,
        gate=VERIFYING,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=lambda _e, _d: desktop.launch(),
            sleeper=lambda s: slept.append(s),
            clock=clock,
        ),
    )
    # One poll is spent on the initial enumeration, so dying after two
    # leaves exactly one down-confirmation sleep.
    assert slept == [waits.down_poll_seconds]
    assert waits.settle_seconds not in slept


def test_survey_distinguishes_no_processes_from_no_answer():
    empty = survey_processes(lambda _cmd: CompletedLike(stdout="[]"))
    assert empty.readable is True
    assert empty.processes == ()

    for broken in (
        CompletedLike(stdout="", returncode=1),
        CompletedLike(stdout="not json at all"),
        CompletedLike(stdout="   "),
    ):
        assert survey_processes(lambda _cmd, b=broken: b).readable is False


def test_survey_reads_a_command_line_holding_a_raw_control_character():
    # ConvertTo-Json emits control characters raw instead of escaping them, and a
    # real Claude command line carried a literal \x07 in --desktop-managed-config.
    # Strict JSON rejects the whole document over that one byte, which read as
    # "unreadable" and blocked every restart on the machine.
    stdout = (
        '[{"ProcessId": 4321,'
        ' "ExecutablePath": "C:\\\\Program Files\\\\WindowsApps'
        '\\\\Claude_1.0.0.0_x64__abc\\\\app\\\\claude.exe",'
        ' "CommandLine": "claude.exe --desktop-managed-config=\x07'
        ' --user-data-dir=C:\\\\data"}]'
    )
    report = survey_processes(lambda _cmd: CompletedLike(stdout=stdout))
    assert report.readable is True
    assert [process.pid for process in report.processes] == [4321]
    assert report.processes[0].profile_dir == "C:\\data"


def test_survey_treats_a_runner_that_raises_as_unreadable():
    def explode(_cmd):
        raise OSError("powershell is not there")

    assert survey_processes(explode).readable is False


def test_cli_reports_a_blocked_restart_distinctly(capsys, monkeypatch, tmp_path):
    import hard_restart_claude_code.cli as cli_module

    def blocked(*_args, **_kwargs):
        raise RestartBlocked("cannot tell what is running")

    monkeypatch.setattr(cli_module, "hard_restart", blocked)
    exe = _exe(tmp_path)
    code = main(["--exe", str(exe), "--verify"])
    assert code == EXIT_CANNOT_CONFIRM
    assert "cannot tell" in capsys.readouterr().err
