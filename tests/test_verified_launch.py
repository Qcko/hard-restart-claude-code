from __future__ import annotations

import pytest

from hard_restart_claude_code.restart import (
    ClaudeProcess,
    Effects,
    LaunchHandle,
    PackageGate,
    PackageReport,
    ProcessReport,
    RestartBlocked,
    Waits,
    hard_restart,
)

VERIFYING = PackageGate(enabled=True, budget_seconds=0.0)

# Small, explicit, and nothing like the production defaults: a test that waits
# 30s per attempt against a fake clock is a test nobody reads the numbers of.
WAITS = Waits(
    down_timeout_seconds=5.0,
    down_poll_seconds=0.1,
    up_timeout_seconds=10.0,
    up_poll_seconds=1.0,
    exit_grace_seconds=4.0,
    launch_attempts=3,
    launch_backoff_seconds=2.0,
    launch_backoff_cap_seconds=5.0,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Desktop:
    """A Desktop that dies when killed and comes back on the launch you nominate.

    `appears_after` is how many up-polls it stays invisible for after that
    launch, which is the difference between a slow start and a dead one.
    """

    def __init__(
        self,
        working_launch: int | None = 1,
        appears_after: int = 0,
        child_alive: bool = False,
        handle: bool = True,
    ) -> None:
        self.alive = True
        self.killed: list[int] = []
        self.launches: list = []
        self.polls_since_launch = 0
        self.working_launch = working_launch
        self.appears_after = appears_after
        self.child_alive = child_alive
        self.handle = handle

    def find(self) -> ProcessReport:
        if self.alive:
            return ProcessReport(True, (ClaudeProcess(pid=1),))
        if self.launches:
            self.polls_since_launch += 1
            if self._launch_took_hold():
                self.alive = True
                return ProcessReport(True, (ClaudeProcess(pid=1),))
        return ProcessReport(True, ())

    def _launch_took_hold(self) -> bool:
        if self.working_launch is None:
            return False
        return (
            len(self.launches) >= self.working_launch
            and self.polls_since_launch > self.appears_after
        )

    def kill(self, pids) -> None:
        self.killed.extend(pids)
        self.alive = False

    def launch(self, exe, _dir=None):
        self.launches.append(exe)
        self.polls_since_launch = 0
        if not self.handle:
            return None
        return LaunchHandle(running=lambda: self.child_alive)


def _exe(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    return exe


def _restart(desktop: Desktop, tmp_path, *, gate=VERIFYING, waits=WAITS, clock=None):
    clock = clock or Clock()
    return hard_restart(
        _exe(tmp_path),
        waits=waits,
        gate=gate,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=desktop.launch,
            sleeper=clock.sleep,
            clock=clock,
        ),
    )


def test_a_launch_that_works_is_confirmed_on_the_first_attempt(tmp_path):
    desktop = Desktop()
    result = _restart(desktop, tmp_path)
    assert result.launched is True
    assert result.attempts == 1
    assert len(desktop.launches) == 1


# The MSIX hand-off: the process we spawned is gone within a second on a
# perfectly good launch. Reading that exit as failure would condemn a start that
# worked and put a second Desktop on top of it.
def test_a_child_that_exits_while_desktop_starts_is_not_a_failure(tmp_path):
    desktop = Desktop(appears_after=2, child_alive=False)
    result = _restart(desktop, tmp_path)
    assert result.attempts == 1
    assert len(desktop.launches) == 1


# The child exiting must not become the real deadline. On MSIX it always exits,
# so bailing out at the grace would cap the wait at a few seconds and spawn a
# second Desktop on top of a first one that was merely slow.
def test_a_slow_start_is_waited_out_even_though_the_child_is_long_gone(tmp_path):
    # Appears well past exit_grace_seconds, but inside up_timeout_seconds.
    desktop = Desktop(appears_after=6, child_alive=False)
    result = _restart(desktop, tmp_path)
    assert result.attempts == 1
    assert len(desktop.launches) == 1


def test_a_dead_launch_is_retried_until_desktop_appears(tmp_path):
    clock = Clock()
    desktop = Desktop(working_launch=2, child_alive=False)
    result = _restart(desktop, tmp_path, clock=clock)
    assert result.attempts == 2
    assert len(desktop.launches) == 2
    # The backoff really happened, and grew with the attempt.
    assert WAITS.launch_backoff_seconds in clock.slept


# THE rule. Getting this wrong produces the worst outcome in the design: two
# Desktops, on two data dirs, both writing session credentials.
def test_a_still_running_child_is_never_relaunched(tmp_path):
    desktop = Desktop(working_launch=None, child_alive=True)
    with pytest.raises(RestartBlocked, match="gave up waiting"):
        _restart(desktop, tmp_path)
    assert len(desktop.launches) == 1


# A launcher that reports nothing leaves the child's fate unknown, and unknown
# has to read as "still alive" - the other answer unlocks exactly the retry
# above.
def test_an_unreported_child_is_treated_as_still_running(tmp_path):
    desktop = Desktop(working_launch=None, handle=False)
    with pytest.raises(RestartBlocked, match="gave up waiting"):
        _restart(desktop, tmp_path)
    assert len(desktop.launches) == 1


def test_a_handle_that_raises_is_treated_as_still_running(tmp_path):
    def explode():
        raise OSError("the handle is gone")

    desktop = Desktop(working_launch=None)
    desktop.launch = lambda exe, _dir=None: (
        desktop.launches.append(exe),
        LaunchHandle(running=explode),
    )[1]
    with pytest.raises(RestartBlocked, match="gave up waiting"):
        _restart(desktop, tmp_path)
    assert len(desktop.launches) == 1


def test_going_blind_during_the_up_wait_stops_rather_than_guesses(tmp_path):
    desktop = Desktop(working_launch=None, child_alive=False)

    def blind_after_launch() -> ProcessReport:
        if desktop.launches:
            return ProcessReport(readable=False)
        return desktop.find()

    clock = Clock()
    with pytest.raises(RestartBlocked, match="lost sight"):
        hard_restart(
            _exe(tmp_path),
            waits=WAITS,
            gate=VERIFYING,
            effects=Effects(
                finder=blind_after_launch,
                killer=desktop.kill,
                launcher=desktop.launch,
                sleeper=clock.sleep,
                clock=clock,
            ),
        )
    assert len(desktop.launches) == 1


def test_the_retries_are_bounded_and_report_what_was_killed(tmp_path):
    desktop = Desktop(working_launch=None, child_alive=False)
    with pytest.raises(RestartBlocked, match="after 3 attempts") as err:
        _restart(desktop, tmp_path)
    assert len(desktop.launches) == WAITS.launch_attempts
    # Desktop is down and did not come back, which a caller must be able to tell
    # from "nothing happened, safe to retry".
    assert err.value.killed == [1]


# One deadline for the restart, not one per attempt: recomputed per attempt a
# stuck package would hold the restart for the budget times the attempt count.
def test_the_package_budget_is_spent_once_across_all_attempts(tmp_path):
    clock = Clock()
    reads: list[int] = []

    def servicing() -> PackageReport:
        reads.append(1)
        return PackageReport(readable=True, packages=())

    gate = PackageGate(
        enabled=True,
        budget_seconds=6.0,
        poll_seconds=1.0,
        reader=servicing,
        clock=clock,
        sleeper=clock.sleep,
    )
    desktop = Desktop(working_launch=None, child_alive=False)
    with pytest.raises(RestartBlocked):
        _restart(desktop, tmp_path, gate=gate, clock=clock)
    assert len(desktop.launches) == WAITS.launch_attempts
    # Six polls, not eighteen - the later attempts find the budget already gone.
    assert len(reads) == 6


# A bare hrcc stays the one-second command a human typed. Inverting this turns
# every restart into one that can block for minutes.
def test_the_unhardened_path_launches_once_and_never_waits(tmp_path):
    clock = Clock()
    desktop = Desktop(working_launch=None, child_alive=False)
    result = hard_restart(
        _exe(tmp_path),
        waits=WAITS,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=lambda exe, _dir: desktop.launches.append(exe),
            sleeper=clock.sleep,
            clock=clock,
        ),
    )
    assert result.launched is True
    assert result.attempts == 1
    assert len(desktop.launches) == 1
    assert clock.slept == [WAITS.settle_seconds]


def test_the_backoff_is_capped(tmp_path):
    clock = Clock()
    waits = Waits(
        down_timeout_seconds=5.0,
        down_poll_seconds=0.1,
        up_timeout_seconds=1.0,
        up_poll_seconds=1.0,
        exit_grace_seconds=0.0,
        launch_attempts=6,
        launch_backoff_seconds=3.0,
        launch_backoff_cap_seconds=5.0,
    )
    desktop = Desktop(working_launch=None, child_alive=False)
    with pytest.raises(RestartBlocked):
        _restart(desktop, tmp_path, waits=waits, clock=clock)
    assert max(clock.slept) == waits.launch_backoff_cap_seconds


def test_launch_settings_reject_values_that_would_spin_or_never_try(tmp_path):
    with pytest.raises(ValueError, match="poll interval"):
        Waits(up_poll_seconds=0)
    with pytest.raises(ValueError, match="at least one attempt"):
        Waits(launch_attempts=0)
    with pytest.raises(ValueError, match="must not be negative"):
        Waits(up_timeout_seconds=-1)
    with pytest.raises(ValueError, match="must not be negative"):
        Waits(exit_grace_seconds=-1)
    # A negative backoff would reach the real time.sleep and raise from inside
    # the retry loop, with Desktop already down.
    with pytest.raises(ValueError, match="backoff must not be negative"):
        Waits(launch_backoff_seconds=-1)


# The exe vanishing from under us mid-update is the situation the hardened path
# exists to ride out, so it is a failed attempt rather than the end of the
# restart - and when it does end the restart, it ends it the way every other
# post-kill refusal does: carrying the pids it already killed.
def test_a_vanished_exe_is_retried_rather_than_fatal(tmp_path):
    exe = _exe(tmp_path)
    desktop = Desktop(child_alive=False)
    clock = Clock()
    exe.unlink()

    # The package comes back between attempts, which is exactly what waiting out
    # an MSIX update looks like from here.
    def sleep_and_restore(seconds: float) -> None:
        clock.sleep(seconds)
        exe.write_text("")

    result = hard_restart(
        exe,
        waits=WAITS,
        gate=VERIFYING,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=desktop.launch,
            sleeper=sleep_and_restore,
            clock=clock,
        ),
    )
    assert result.attempts == 2
    assert len(desktop.launches) == 1


def test_a_permanently_missing_exe_still_reports_what_was_killed(tmp_path):
    exe = _exe(tmp_path)
    desktop = Desktop(working_launch=None)
    exe.unlink()
    clock = Clock()
    with pytest.raises(RestartBlocked, match="exe not found") as err:
        hard_restart(
            exe,
            waits=WAITS,
            gate=VERIFYING,
            effects=Effects(
                finder=desktop.find,
                killer=desktop.kill,
                launcher=desktop.launch,
                sleeper=clock.sleep,
                clock=clock,
            ),
        )
    assert desktop.launches == []
    assert err.value.killed == [1]
    assert err.value.attempts == WAITS.launch_attempts


# A caller that has to read the attempt count out of the prose is back to
# regex-scraping, which is what --json exists to abolish.
def test_a_blocked_launch_carries_the_attempt_count(tmp_path):
    desktop = Desktop(working_launch=None, child_alive=True)
    with pytest.raises(RestartBlocked) as err:
        _restart(desktop, tmp_path)
    assert err.value.attempts == 1


# The unhardened path has no second attempt to offer, so a missing exe stays the
# plain error it has always been, with the exit code callers already handle.
def test_the_unhardened_path_still_errors_on_a_missing_exe(tmp_path):
    exe = _exe(tmp_path)
    exe.unlink()
    desktop = Desktop()
    with pytest.raises(FileNotFoundError):
        hard_restart(
            exe,
            waits=WAITS,
            effects=Effects(
                finder=desktop.find,
                killer=desktop.kill,
                launcher=desktop.launch,
                sleeper=lambda _s: None,
            ),
        )
