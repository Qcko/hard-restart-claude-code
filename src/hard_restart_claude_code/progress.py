from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# A hardened restart kills Desktop and then has to get it back, which takes tens
# of seconds and can fail with nobody watching - the terminal that launched it is
# usually inside the Desktop being killed. hrcc therefore WRITES what it is doing
# to a file. It does not own the reader and it never spawns a UI: the widget that
# reads this lives in the calling project, where it coordinates with that
# project's other widgets.
SCHEMA_VERSION = 1

PHASE_STOPPING = "stopping"
PHASE_WAITING_DOWN = "waiting-down"
PHASE_WAITING_PACKAGE = "waiting-package"
PHASE_LAUNCHING = "launching"
PHASE_WAITING_UP = "waiting-up"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

PHASES = (
    PHASE_STOPPING,
    PHASE_WAITING_DOWN,
    PHASE_WAITING_PACKAGE,
    PHASE_LAUNCHING,
    PHASE_WAITING_UP,
    PHASE_DONE,
    PHASE_FAILED,
)

STATE_FILE_NAME = "restart-status.json"
TRACE_SUFFIX = ".trace.jsonl"
STATE_DIR_NAME = ".hrcc"


# Deliberately under the home directory rather than %LOCALAPPDATA%. hrcc runs
# inside Claude Desktop's MSIX package context, where %LOCALAPPDATA%\hrcc turned
# out to be a reparse point into the package container - state written there was
# invisible to anything running outside it, and the failure was silent.
def default_progress_file() -> Path:
    return Path.home() / STATE_DIR_NAME / STATE_FILE_NAME


# The PowerShell reader parses this with [datetime] and compares it against
# Get-Date, which is LOCAL. A naive UTC string is therefore read as local, puts
# every frame past the staleness cutoff, and parks the widget on "waiting to
# start" for the whole restart. An explicit offset is the fix, and the exact
# string is unit-tested because nothing else would catch it going naive.
def timestamp(now: Callable[[], datetime] | None = None) -> str:
    moment = (now or datetime.now)()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec="seconds")


def _ignore(_message: str) -> None:
    pass


@dataclass
class Progress:
    """Publishes the current phase, and never lets that failure reach the restart."""

    file: Path | None = None
    label: str | None = None
    max_attempts: int = 0
    log: Callable[[str], None] = _ignore
    started_at: str = field(default_factory=timestamp)
    _rename_works: bool = True

    def publish(self, phase: str, detail: str, **extra) -> None:
        if self.file is None:
            return
        try:
            self._write(self._record(phase, detail, extra))
        except Exception as err:
            # Including an unknown phase from a future call site: a mislabelled
            # status must never become a failed restart.
            self.warn(f"could not publish restart progress ({err})")

    def _record(self, phase: str, detail: str, extra: dict) -> dict:
        if phase not in PHASES:
            raise ValueError(f"unknown restart phase '{phase}'")
        record = {
            "schemaVersion": SCHEMA_VERSION,
            "pid": os.getpid(),
            "label": self.label,
            "maxAttempts": self.max_attempts,
            "startedAt": self.started_at,
            "updatedAt": timestamp(),
            "phase": phase,
            "detail": detail,
            "attempt": 0,
            "packageStatus": None,
            "error": None,
        }
        record.update(extra)
        return record

    def _write(self, record: dict) -> None:
        payload = json.dumps(record, indent=2)
        self._write_state(payload)
        self._append_trace(record)

    # Rename-over-temp is the atomic write, so a reader can never see half a
    # file. It does not work everywhere: under the MSIX container, writes to the
    # local app-data path are virtualized and the rename fails every single time,
    # which once silently disabled this whole channel. Catch broadly rather than
    # testing an errno - under the filter driver it is not reliable - and fall
    # back permanently, because once is enough to know.
    def _write_state(self, payload: str) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if not self._rename_works:
            self._overwrite(payload)
            return
        temp = self.file.with_suffix(self.file.suffix + ".tmp")
        try:
            _write_text(temp, payload)
            os.replace(temp, self.file)
        except Exception:
            self._rename_works = False
            self._overwrite(payload)
            _discard(temp)

    def _overwrite(self, payload: str) -> None:
        _write_text(self.file, payload)

    # The state file is a single slot rewritten on every publish, so it is
    # evidence of the CURRENT phase and never of the run: a fast success looks
    # exactly like a run where the package gate never fired. The trace is what
    # makes a finished restart readable afterwards.
    def _append_trace(self, record: dict) -> None:
        try:
            with open(self._trace_file(), "a", encoding="utf-8", newline="\n") as out:
                out.write(json.dumps(record) + "\n")
        except Exception as err:
            self.warn(f"could not append to the restart trace ({err})")

    # A fresh trace per run: it exists to explain one restart, and a file that
    # grew across every restart would need pruning nobody would write.
    def begin(self) -> None:
        if self.file is None:
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        _discard(self._trace_file())

    def _trace_file(self) -> Path:
        return self.file.with_suffix(self.file.suffix + TRACE_SUFFIX)

    # This process is usually orphaned by the very kill it performs, so its
    # stdout and stderr are dead pipes for most of the run. A warning about a
    # failed write must not itself take the restart down.
    def warn(self, message: str) -> None:
        try:
            self.log(f"warning: {message}")
        except Exception:
            pass


# UTF-8 with no BOM, and \n line endings, because the reader is PowerShell and
# ConvertFrom-Json chokes on a BOM it did not expect.
def _write_text(path: Path, payload: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        out.write(payload)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


NO_PROGRESS = Progress()


def open_progress(
    file: Path | None,
    label: str | None = None,
    max_attempts: int = 0,
    log: Callable[[str], None] = _ignore,
) -> Progress:
    if file is None:
        return NO_PROGRESS
    progress = Progress(file=file, label=label, max_attempts=max_attempts, log=log)
    try:
        progress.begin()
    except Exception as err:
        # Nowhere to publish to. The restart is unaffected, so carry on with a
        # publisher that does nothing rather than taking the restart down.
        progress.warn(f"restart progress is unavailable ({err})")
        return NO_PROGRESS
    return progress
