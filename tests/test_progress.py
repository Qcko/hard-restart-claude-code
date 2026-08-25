from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hard_restart_claude_code.cli import build_parser, build_progress
from hard_restart_claude_code.progress import (
    NO_PROGRESS,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_LAUNCHING,
    PHASE_STOPPING,
    PHASE_WAITING_DOWN,
    PHASE_WAITING_PACKAGE,
    PHASE_WAITING_UP,
    SCHEMA_VERSION,
    Progress,
    open_progress,
    timestamp,
)
from hard_restart_claude_code.restart import (
    ClaudeProcess,
    Effects,
    LaunchHandle,
    PackageGate,
    ProcessReport,
    RestartBlocked,
    Waits,
    hard_restart,
)

VERIFYING = PackageGate(enabled=True, budget_seconds=0.0)
WAITS = Waits(
    down_timeout_seconds=5.0,
    down_poll_seconds=0.1,
    up_timeout_seconds=10.0,
    up_poll_seconds=1.0,
    exit_grace_seconds=4.0,
    launch_attempts=2,
    launch_backoff_seconds=1.0,
    launch_backoff_cap_seconds=1.0,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Desktop:
    def __init__(self, comes_back: bool = True) -> None:
        self.alive = True
        self.comes_back = comes_back
        self.launched = False

    def find(self) -> ProcessReport:
        if self.alive:
            return ProcessReport(True, (ClaudeProcess(pid=1),))
        if self.launched and self.comes_back:
            self.alive = True
            return ProcessReport(True, (ClaudeProcess(pid=1),))
        return ProcessReport(True, ())

    def kill(self, _pids) -> None:
        self.alive = False

    def launch(self, _exe, _dir=None):
        self.launched = True
        return LaunchHandle(running=lambda: False)


def _progress(tmp_path, **overrides) -> Progress:
    settings = dict(file=tmp_path / "restart-status.json", label="reserve")
    settings.update(overrides)
    return Progress(**settings)


def _state(progress: Progress) -> dict:
    return json.loads(progress.file.read_text(encoding="utf-8"))


def _trace(progress: Progress) -> list[dict]:
    path = progress.file.with_suffix(progress.file.suffix + ".trace.jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# The PowerShell reader parses this with [datetime] and compares it against
# Get-Date, which is local. A naive UTC string is read as local time, which puts
# every frame past the staleness cutoff and parks the widget on "waiting to
# start" for the entire restart. Nothing but this assertion would catch it.
def test_the_timestamp_always_carries_an_explicit_offset():
    assert re.search(r"([+-]\d{2}:\d{2}|Z)$", timestamp())
    naive = timestamp(lambda: datetime(2026, 8, 25, 21, 14, 3))
    assert re.search(r"([+-]\d{2}:\d{2}|Z)$", naive)
    aware = timestamp(lambda: datetime(2026, 8, 25, 21, 14, 3, tzinfo=timezone.utc))
    assert aware == "2026-08-25T21:14:03+00:00"
    # And the offset has to describe the clock it is attached to: a naive UTC
    # reading stamped with the LOCAL offset carries a suffix and still lies.
    assert naive.startswith("2026-08-25T21:14:03")


def test_the_record_carries_the_documented_shape(tmp_path):
    progress = _progress(tmp_path, max_attempts=8)
    progress.publish(PHASE_LAUNCHING, "Starting Claude Desktop", attempt=2)
    state = _state(progress)
    assert state["schemaVersion"] == SCHEMA_VERSION
    assert state["phase"] == PHASE_LAUNCHING
    assert state["detail"] == "Starting Claude Desktop"
    assert state["label"] == "reserve"
    assert state["maxAttempts"] == 8
    assert state["packageStatus"] is None
    assert state["error"] is None
    # Integers stay integers: a reader that has to parse "2" is a reader that
    # will one day forget to.
    assert state["attempt"] == 2 and isinstance(state["attempt"], int)
    assert isinstance(state["pid"], int)


def test_the_file_is_utf8_without_a_bom(tmp_path):
    progress = _progress(tmp_path)
    progress.publish(PHASE_DONE, "Claude Desktop is running")
    raw = progress.file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw


# Strict writer, lenient reader. A mislabelled status from a future call site is
# a bug worth refusing to write - and never a failed restart.
def test_an_unknown_phase_is_refused_without_breaking_the_restart(tmp_path):
    warnings: list[str] = []
    progress = _progress(tmp_path, log=warnings.append)
    progress.publish(PHASE_DONE, "fine")
    progress.publish("teleporting", "not a phase")
    assert _state(progress)["phase"] == PHASE_DONE
    assert any("teleporting" in w for w in warnings)


# MSIX virtualization of the local app-data path made this rename fail every
# single time, which silently disabled the whole status channel.
def test_a_failing_rename_falls_back_to_a_plain_overwrite(tmp_path, monkeypatch):
    progress = _progress(tmp_path)
    calls: list[int] = []

    def refuse(*_args, **_kwargs):
        calls.append(1)
        raise OSError("EXDEV")

    monkeypatch.setattr(os, "replace", refuse)
    progress.publish(PHASE_STOPPING, "Stopping Claude Desktop")
    progress.publish(PHASE_DONE, "Claude Desktop is running")
    assert _state(progress)["phase"] == PHASE_DONE
    # Once is enough to know: the second publish does not try the rename again,
    # and no temp file is left behind on every tick.
    assert len(calls) == 1
    # Per-pid, so two runs racing on one path cannot consume each other's temp -
    # and nothing is left behind on every tick.
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_publish_that_cannot_write_never_reaches_the_restart(tmp_path):
    warnings: list[str] = []
    # A file where the directory should be: every write below fails.
    blocked = tmp_path / "wall"
    blocked.write_text("")
    progress = Progress(file=blocked / "restart-status.json", log=warnings.append)
    progress.publish(PHASE_STOPPING, "Stopping Claude Desktop")
    assert warnings


# hrcc is orphaned by the very kill it performs, so its stderr is a dead pipe for
# most of a hardened run.
def test_a_log_that_raises_is_swallowed(tmp_path):
    def dead_pipe(_message: str) -> None:
        raise OSError("the pipe is gone")

    progress = _progress(tmp_path, log=dead_pipe)
    progress.publish("teleporting", "not a phase")


# The state file is a single slot rewritten on every publish, so it is evidence
# of the current phase and never of the run.
def test_the_trace_keeps_what_the_state_file_overwrites(tmp_path):
    progress = _progress(tmp_path)
    progress.begin()
    progress.publish(PHASE_STOPPING, "Stopping Claude Desktop")
    progress.publish(PHASE_DONE, "Claude Desktop is running")
    assert [record["phase"] for record in _trace(progress)] == [
        PHASE_STOPPING,
        PHASE_DONE,
    ]
    assert _state(progress)["phase"] == PHASE_DONE


def test_each_run_starts_a_fresh_trace(tmp_path):
    first = open_progress(tmp_path / "restart-status.json")
    first.publish(PHASE_STOPPING, "Stopping Claude Desktop")
    second = open_progress(tmp_path / "restart-status.json")
    second.publish(PHASE_DONE, "Claude Desktop is running")
    assert [record["phase"] for record in _trace(second)] == [PHASE_DONE]


def test_no_file_means_no_publishing(tmp_path):
    assert open_progress(None) is NO_PROGRESS
    NO_PROGRESS.publish(PHASE_DONE, "nothing happens")
    assert list(tmp_path.iterdir()) == []


def _restart(tmp_path, progress, desktop, exe=None):
    clock = Clock()
    return hard_restart(
        exe or _exe(tmp_path),
        waits=WAITS,
        gate=VERIFYING,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=desktop.launch,
            sleeper=clock.sleep,
            clock=clock,
            progress=progress,
        ),
    )


def _exe(tmp_path) -> Path:
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    return exe


def test_a_hardened_restart_publishes_every_phase_in_order(tmp_path):
    progress = _progress(tmp_path)
    progress.begin()
    _restart(tmp_path, progress, Desktop())
    assert [record["phase"] for record in _trace(progress)] == [
        PHASE_STOPPING,
        PHASE_WAITING_DOWN,
        PHASE_LAUNCHING,
        PHASE_WAITING_UP,
        PHASE_DONE,
    ]


# A restart that gives up must leave that on screen. One that only raises leaves
# the widget showing the last thing that went right.
def test_a_failed_restart_publishes_its_failure(tmp_path):
    progress = _progress(tmp_path)
    progress.begin()
    with pytest.raises(RestartBlocked):
        _restart(tmp_path, progress, Desktop(comes_back=False))
    state = _state(progress)
    assert state["phase"] == PHASE_FAILED
    assert state["error"]


# Published strings stay path-free: a profile path names a private directory, and
# this file is read by a widget that puts it on screen.
def test_published_strings_never_carry_a_path(tmp_path):
    progress = _progress(tmp_path)
    progress.begin()
    exe = _exe(tmp_path)
    exe.unlink()
    with pytest.raises(RestartBlocked):
        _restart(tmp_path, progress, Desktop(comes_back=False), exe=exe)
    for record in _trace(progress):
        published = f"{record['detail']} {record['error']}"
        assert str(tmp_path) not in published
        assert "claude.exe" not in published


def _args(argv: list[str]):
    return build_parser().parse_args(argv)


# A dry run changes nothing, and a state file is a change.
def test_a_dry_run_publishes_nothing(tmp_path):
    progress = build_progress(_args(["--dry-run", "--verify"]))
    assert progress is NO_PROGRESS


def test_an_explicit_progress_file_wins(tmp_path):
    target = tmp_path / "elsewhere" / "status.json"
    progress = build_progress(_args(["--verify", "--progress-file", str(target), "--label", "reserve"]))
    assert progress.file == target
    assert progress.label == "reserve"


# The default is hrcc's OWN directory. A consumer's path in this tool's source
# would be the dependency inversion the profile design already refuses.
def test_verifying_defaults_the_file_into_hrccs_own_directory(tmp_path, monkeypatch):
    import hard_restart_claude_code.cli as cli_module

    default = tmp_path / ".hrcc" / "restart-status.json"
    monkeypatch.setattr(cli_module, "default_progress_file", lambda: default)
    progress = build_progress(_args(["--verify"]))
    assert progress.file == default


def test_a_bare_restart_publishes_nothing_at_all():
    assert build_progress(_args([])) is NO_PROGRESS


# Strict writer, past the phase name too. Without this an extra could overwrite
# the phase that was just validated, and a typo would add a junk field rather
# than being refused.
def test_a_publish_cannot_smuggle_in_unknown_fields(tmp_path):
    warnings: list[str] = []
    progress = _progress(tmp_path, log=warnings.append)
    progress.publish(PHASE_DONE, "fine")
    progress.publish(PHASE_LAUNCHING, "starting", packagestatus="Disabled")
    progress.publish(PHASE_LAUNCHING, "starting", schemaVersion=99)
    assert _state(progress)["phase"] == PHASE_DONE
    assert len(warnings) == 2


# The package gate has no attempt of its own. Defaulting it to zero wound a
# reader's counter back to "attempt 0 of 8" in the middle of attempt three.
def test_a_package_frame_does_not_wind_the_attempt_back(tmp_path):
    progress = _progress(tmp_path, max_attempts=8)
    progress.publish(PHASE_LAUNCHING, "Starting Claude Desktop", attempt=3)
    progress.publish(PHASE_WAITING_PACKAGE, "Waiting", packageStatus="Disabled")
    assert _state(progress)["attempt"] == 3


# The per-run trace has to hold however the publisher was built, or a second run
# interleaves into the first one's file with nothing separating them.
def test_the_trace_is_fresh_even_without_begin(tmp_path):
    first = _progress(tmp_path)
    first.publish(PHASE_STOPPING, "Stopping Claude Desktop")
    second = _progress(tmp_path)
    second.publish(PHASE_DONE, "Claude Desktop is running")
    assert [record["phase"] for record in _trace(second)] == [PHASE_DONE]


def test_a_non_ascii_label_survives_the_round_trip(tmp_path):
    progress = _progress(tmp_path, label="r\u00e9serve")
    progress.publish(PHASE_DONE, "Claude Desktop is running")
    assert _state(progress)["label"] == "r\u00e9serve"


def _unhardened(tmp_path, progress, desktop, exe=None):
    clock = Clock()
    return hard_restart(
        exe or _exe(tmp_path),
        waits=WAITS,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=desktop.launch,
            sleeper=clock.sleep,
            clock=clock,
            progress=progress,
        ),
    )


# A progress file parked on "stopping" forever is worse than no progress file:
# the reader cannot tell a finished restart from a hung one.
def test_an_unverified_run_still_reaches_a_terminal_phase(tmp_path):
    progress = _progress(tmp_path)
    _unhardened(tmp_path, progress, Desktop())
    assert _state(progress)["phase"] == PHASE_DONE


def test_stopping_without_launching_reaches_a_terminal_phase(tmp_path):
    progress = _progress(tmp_path)
    clock = Clock()
    desktop = Desktop()
    hard_restart(
        _exe(tmp_path),
        no_launch=True,
        waits=WAITS,
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=desktop.launch,
            sleeper=clock.sleep,
            clock=clock,
            progress=progress,
        ),
    )
    assert _state(progress)["phase"] == PHASE_DONE


def test_an_unverified_missing_exe_publishes_a_path_free_failure(tmp_path):
    progress = _progress(tmp_path)
    exe = _exe(tmp_path)
    exe.unlink()
    with pytest.raises(FileNotFoundError):
        _unhardened(tmp_path, progress, Desktop(), exe=exe)
    state = _state(progress)
    assert state["phase"] == PHASE_FAILED
    assert str(tmp_path) not in f"{state['detail']} {state['error']}"
