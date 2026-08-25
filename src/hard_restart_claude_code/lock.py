from __future__ import annotations

import ctypes
from collections.abc import Callable
from dataclasses import dataclass

# Two hardened restarts must not overlap. Each one kills every Claude Desktop
# process it can see and then spends up to minutes putting one back, so a second
# run starting mid-flight kills the Desktop the first one just launched and both
# then race to relaunch - the two-Desktop outcome, reached from the other end.
#
# A Windows named mutex rather than a lock file, because this process is
# routinely killed or orphaned by the very restart it performs: the kernel drops
# the handle when the process dies, so there is no stale lock to reason about and
# nothing to clean up after a crash. The "Local\\" namespace scopes it to the
# logon session, which is the scope Desktop itself runs in.
MUTEX_NAME = "Local\\hrcc-hardened-restart"

ERROR_ALREADY_EXISTS = 183

Creator = Callable[[str], "tuple[int, int]"]


# `busy` is the answer that stops a restart; a handle of None with busy False is
# "could not tell", which deliberately proceeds. A lock that cannot be taken must
# never become the reason a working restart does not happen - the same rule the
# package gate follows, for the same reason.
@dataclass
class RunLock:
    handle: int | None = None
    busy: bool = False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            ctypes.windll.kernel32.CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None


BUSY = RunLock(busy=True)


def acquire_run_lock(
    name: str = MUTEX_NAME, creator: Creator | None = None
) -> RunLock:
    try:
        handle, error = (creator or _create_mutex)(name)
    except Exception:
        return RunLock()
    if error == ERROR_ALREADY_EXISTS:
        _close(handle)
        return RunLock(busy=True)
    if not handle:
        return RunLock()
    return RunLock(handle=handle)


# The handle is kept for the life of the run: the mutex object exists for as long
# as any process holds one, which is what makes its mere existence mean "a run is
# in progress".
def _create_mutex(name: str) -> tuple[int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    return handle, ctypes.get_last_error()


# A handle returned alongside ERROR_ALREADY_EXISTS is a valid second handle to
# the same mutex, and leaking it would keep the object alive past our exit -
# making every later run look busy.
def _close(handle: int | None) -> None:
    if not handle:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
