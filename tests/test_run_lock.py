from __future__ import annotations

import json

import pytest

from hard_restart_claude_code import cli as cli_module
from hard_restart_claude_code import lock as lock_module
from hard_restart_claude_code.cli import EXIT_ALREADY_RUNNING, EXIT_OK, main, take_run_lock
from hard_restart_claude_code.lock import (
    ERROR_ALREADY_EXISTS,
    MUTEX_NAME,
    RunLock,
    acquire_run_lock,
)

TEST_MUTEX = MUTEX_NAME + "-test"


def _args(argv: list[str]):
    return cli_module.build_parser().parse_args(argv)


def _exe(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    return exe


# Against the real kernel, not a fake: the whole point of a named mutex over a
# lock file is that Windows owns its lifetime, and a fake cannot show that.
def test_a_second_run_is_refused_and_the_lock_is_released_on_exit():
    first = acquire_run_lock(TEST_MUTEX)
    assert first.handle and not first.busy
    try:
        assert acquire_run_lock(TEST_MUTEX).busy is True
    finally:
        first.release()
    # Released, so the next run may have it. This is also what happens when hrcc
    # is killed mid-restart: the kernel drops the handle and no stale lock is
    # left behind for anyone to clear.
    second = acquire_run_lock(TEST_MUTEX)
    assert second.handle and not second.busy
    second.release()


def test_releasing_twice_is_harmless():
    lock = acquire_run_lock(TEST_MUTEX)
    lock.release()
    lock.release()
    assert lock.handle is None


# A lock that cannot be taken must never be the reason a working restart does not
# happen - the same rule the package gate follows.
def test_a_lock_that_cannot_be_taken_lets_the_restart_proceed():
    def no_handle(_name):
        return 0, 0

    def explode(_name):
        raise OSError("no kernel32 here")

    for creator in (no_handle, explode):
        lock = acquire_run_lock(TEST_MUTEX, creator=creator)
        assert lock.busy is False
        assert lock.handle is None


# A handle handed back alongside ERROR_ALREADY_EXISTS is a real second handle to
# the same object. Leaking it would keep the mutex alive past our exit and make
# every later run look busy.
def test_the_losers_handle_is_closed(monkeypatch):
    closed: list[int] = []

    class FakeKernel:
        @staticmethod
        def CloseHandle(handle):
            closed.append(handle)

    monkeypatch.setattr(
        lock_module.ctypes, "windll", type("W", (), {"kernel32": FakeKernel})
    )
    lock = acquire_run_lock(TEST_MUTEX, creator=lambda _n: (4242, ERROR_ALREADY_EXISTS))
    assert lock.busy is True
    assert closed == [4242]


# A bare hrcc is a one-second command that has never coordinated with anything,
# and --dry-run changes nothing by definition.
def test_only_a_verifying_run_takes_the_lock(tmp_path):
    assert take_run_lock(_args([])).handle is None
    assert take_run_lock(_args(["--dry-run", "--verify"])).handle is None
    lock = take_run_lock(_args(["--verify"]))
    assert lock.handle
    lock.release()


def test_a_busy_lock_stops_the_restart_before_anything_is_killed(
    tmp_path, monkeypatch, capsys
):
    restarts: list[int] = []
    monkeypatch.setattr(cli_module, "acquire_run_lock", lambda: RunLock(busy=True))
    monkeypatch.setattr(
        cli_module, "hard_restart", lambda *a, **k: restarts.append(1)
    )
    code = main(["--exe", str(_exe(tmp_path)), "--verify", "--json"])
    assert code == EXIT_ALREADY_RUNNING
    assert restarts == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == EXIT_ALREADY_RUNNING


# Held for the duration and released however the run ends, including the failure
# paths - a lock leaked by an error would block every restart until reboot.
@pytest.mark.parametrize("outcome", ["ok", "raises"])
def test_the_lock_is_released_however_the_run_ends(tmp_path, monkeypatch, outcome):
    released: list[int] = []
    lock = RunLock(handle=1)
    monkeypatch.setattr(lock, "release", lambda: released.append(1))
    monkeypatch.setattr(cli_module, "acquire_run_lock", lambda: lock)

    def run(*_args, **_kwargs):
        if outcome == "raises":
            raise RuntimeError("something nobody predicted")
        return _result(tmp_path)

    monkeypatch.setattr(cli_module, "hard_restart", run)
    if outcome == "raises":
        with pytest.raises(RuntimeError):
            main(["--exe", str(_exe(tmp_path)), "--verify"])
    else:
        assert main(["--exe", str(_exe(tmp_path)), "--verify"]) == EXIT_OK
    assert released == [1]


def _result(tmp_path):
    from hard_restart_claude_code.restart import Result

    return Result(killed=[], launched=True, exe=_exe(tmp_path), attempts=1)
