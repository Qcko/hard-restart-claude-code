from __future__ import annotations

from pathlib import Path

import pytest

from hard_restart_claude_code import restart as restart_module
from hard_restart_claude_code.progress import NO_PROGRESS
from hard_restart_claude_code.cli import build_gate, build_parser
from hard_restart_claude_code.restart import (
    ClaudePackage,
    ClaudeProcess,
    CompletedLike,
    Effects,
    PACKAGE_STATUS_BUDGET_SPENT,
    PACKAGE_STATUS_NOT_REGISTERED,
    PACKAGE_STATUS_OK,
    PACKAGE_STATUS_UNREADABLE,
    PackageGate,
    PackageReport,
    ProcessReport,
    await_package_ready,
    hard_restart,
    package_status_summary,
    read_packages,
    ready_package,
    simulated_reader,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _gate(reader, clock: FakeClock, **overrides) -> PackageGate:
    settings = dict(
        enabled=True,
        budget_seconds=10.0,
        poll_seconds=1.0,
        reader=reader,
        clock=clock,
        sleeper=clock.sleep,
    )
    settings.update(overrides)
    return PackageGate(**settings)


def _record(desktop, launched: list, exe) -> None:
    launched.append(exe)
    desktop.launch(exe)


def _installed(tmp_path, status: str = PACKAGE_STATUS_OK) -> ClaudePackage:
    location = tmp_path / "Claude_1.0.0.0_x64__abc"
    (location / "app").mkdir(parents=True, exist_ok=True)
    (location / "app" / "claude.exe").write_text("")
    return ClaudePackage(status=status, location=str(location))


def _trust_tmp_path(monkeypatch, tmp_path) -> None:
    # ready_package only trusts an exe under the admin-only package root; point
    # that root at the fixture so the trust rule itself stays under test.
    monkeypatch.setattr(
        restart_module, "package_install_prefix", lambda: str(tmp_path) + "\\"
    )


class DyingDesktop:
    """A finder that reports one process until the killer runs, as a real one does."""

    def __init__(self) -> None:
        self.alive = True

    def find(self) -> ProcessReport:
        return ProcessReport(True, [ClaudeProcess(pid=1)] if self.alive else [])

    def kill(self, _pids) -> None:
        self.alive = False

    # The hardened path waits for Desktop to come back, so the fake has to come
    # back. Without this the up-wait sits out its whole timeout on every test.
    def launch(self, exe, _dir=None) -> None:
        self.alive = True


def _gate_for(argv: list[str]):
    return build_gate(build_parser().parse_args(argv), NO_PROGRESS)


# The headline rule: a bare hrcc stays a one-second command. Inverting this
# boolean would quietly turn every restart into a two-minute one.
def test_verification_is_off_unless_asked_for():
    assert _gate_for([]).enabled is False
    assert _gate_for(["--dry-run"]).enabled is False
    assert _gate_for(["--no-launch"]).enabled is False
    assert _gate_for(["--no-profile"]).enabled is False


def test_verification_is_on_when_asked_for_or_implied():
    assert _gate_for(["--verify"]).enabled is True
    assert _gate_for(["--profile-dir", r"D:\p"]).enabled is True
    assert _gate_for(["--simulate-package-status", "Disabled"]).enabled is True


def test_simulating_ok_is_refused():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--simulate-package-status", "ok"])


def test_a_reader_that_raises_is_treated_as_unreadable():
    # The gate runs after the kill. An exception escaping here would leave
    # Desktop dead with the caller told it was an exe problem.
    def explode() -> PackageReport:
        raise FileNotFoundError("powershell is not on PATH")

    clock = FakeClock()
    exe, status = await_package_ready(_gate(explode, clock, budget_seconds=600.0))
    assert exe is None
    assert status == PACKAGE_STATUS_UNREADABLE
    assert clock.slept == []


def test_a_spinning_poll_interval_is_refused():
    with pytest.raises(ValueError):
        PackageGate(enabled=True, poll_seconds=0)


def test_first_serviceable_package_wins_when_two_are_listed(monkeypatch, tmp_path):
    # Mid-update the staged and installed packages are both listed. Unlike
    # discover_exe, which refuses all ambiguity, the gate takes the Ok one.
    _trust_tmp_path(monkeypatch, tmp_path)
    staged = ClaudePackage(status="Disabled", location=str(tmp_path / "Claude_new"))
    installed = _installed(tmp_path)
    assert ready_package([staged, installed]) is installed


def test_a_disabled_gate_never_reads_anything():
    calls = []
    gate = PackageGate(enabled=False, reader=lambda: calls.append(1))
    assert await_package_ready(gate) == (None, None)
    assert calls == []


def test_gate_returns_the_exe_of_a_ready_package(monkeypatch, tmp_path):
    _trust_tmp_path(monkeypatch, tmp_path)
    package = _installed(tmp_path)
    clock = FakeClock()
    exe, status = await_package_ready(
        _gate(lambda: PackageReport(True, (package,)), clock)
    )
    assert exe == package.exe
    assert status == PACKAGE_STATUS_OK
    assert clock.slept == []


def test_gate_waits_out_a_servicing_package_then_launches_it(monkeypatch, tmp_path):
    _trust_tmp_path(monkeypatch, tmp_path)
    servicing = ClaudePackage(status="Disabled", location=str(tmp_path / "Claude_x"))
    ready = _installed(tmp_path)
    reports = [
        PackageReport(True, (servicing,)),
        PackageReport(True, (servicing,)),
        PackageReport(True, (ready,)),
    ]
    seen: list[str] = []
    clock = FakeClock()
    exe, status = await_package_ready(
        _gate(lambda: reports.pop(0), clock, on_status=seen.append)
    )
    assert exe == ready.exe
    assert status == PACKAGE_STATUS_OK
    assert seen == ["Disabled", "Disabled", PACKAGE_STATUS_OK]
    assert clock.slept == [1.0, 1.0]


# Fail open, twice over: an unreadable package state and an exhausted budget
# both let the launch proceed and be judged on its own result.
def test_unreadable_gives_up_immediately_rather_than_burning_the_budget():
    clock = FakeClock()
    exe, status = await_package_ready(
        _gate(lambda: PackageReport(readable=False), clock, budget_seconds=600.0)
    )
    assert exe is None
    assert status == PACKAGE_STATUS_UNREADABLE
    assert clock.slept == []


def test_budget_exhaustion_is_reported_distinctly():
    clock = FakeClock()
    stuck = ClaudePackage(status="Disabled", location="C:\\nope")
    exe, status = await_package_ready(
        _gate(lambda: PackageReport(True, (stuck,)), clock, budget_seconds=3.0)
    )
    assert exe is None
    assert status == PACKAGE_STATUS_BUDGET_SPENT
    assert clock.slept == [1.0, 1.0, 1.0]


def test_ready_package_rejects_an_exe_outside_the_package_root(monkeypatch, tmp_path):
    _trust_tmp_path(monkeypatch, tmp_path)
    elsewhere = tmp_path.parent / "elsewhere"
    (elsewhere / "app").mkdir(parents=True, exist_ok=True)
    (elsewhere / "app" / "claude.exe").write_text("")
    package = ClaudePackage(status=PACKAGE_STATUS_OK, location=str(elsewhere))
    assert ready_package([package]) is None


def test_ready_package_rejects_ok_when_the_exe_is_not_on_disk(monkeypatch, tmp_path):
    _trust_tmp_path(monkeypatch, tmp_path)
    missing = ClaudePackage(status=PACKAGE_STATUS_OK, location=str(tmp_path / "gone"))
    assert ready_package([missing]) is None


def test_status_summary_names_an_empty_package_list():
    assert package_status_summary([]) == PACKAGE_STATUS_NOT_REGISTERED
    assert package_status_summary(
        [ClaudePackage("Disabled", "a"), ClaudePackage("Ok", "b")]
    ) == "Disabled, Ok"


def test_read_packages_reports_a_failed_query_as_unreadable():
    report = read_packages(lambda _cmd: CompletedLike(stdout="", returncode=1))
    assert report.readable is False


def test_read_packages_skips_a_staged_package_with_no_location():
    stdout = '[{"Status":"Ok","InstallLocation":""},{"Status":"Ok","InstallLocation":"C:\\\\p"}]'
    report = read_packages(lambda _cmd: CompletedLike(stdout=stdout))
    assert [p.location for p in report.packages] == ["C:\\p"]


def test_simulated_reader_reports_the_status_it_was_given():
    report = simulated_reader("Disabled")()
    assert report.readable is True
    assert [p.status for p in report.packages] == ["Disabled"]
    assert simulated_reader(PACKAGE_STATUS_UNREADABLE)().readable is False


def test_restart_still_launches_when_the_gate_gives_up(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    clock = FakeClock()
    desktop = DyingDesktop()
    launched = []
    result = hard_restart(
        exe,
        gate=_gate(lambda: PackageReport(readable=False), clock),
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=lambda e, _dir: _record(desktop, launched, e),
            sleeper=clock.sleep,
            clock=clock,
        ),
    )
    assert launched == [exe]
    assert result.launched is True
    assert result.package_status == PACKAGE_STATUS_UNREADABLE


def test_restart_launches_the_exe_the_gate_chose(monkeypatch, tmp_path):
    _trust_tmp_path(monkeypatch, tmp_path)
    package = _installed(tmp_path)
    stale = tmp_path / "stale-claude.exe"
    stale.write_text("")
    clock = FakeClock()
    desktop = DyingDesktop()
    launched: list[Path] = []
    result = hard_restart(
        stale,
        gate=_gate(lambda: PackageReport(True, (package,)), clock),
        effects=Effects(
            finder=desktop.find,
            killer=desktop.kill,
            launcher=lambda e, _dir: _record(desktop, launched, e),
            sleeper=clock.sleep,
            clock=clock,
        ),
    )
    assert launched == [package.exe]
    assert result.exe == package.exe
    assert result.package_status == PACKAGE_STATUS_OK
