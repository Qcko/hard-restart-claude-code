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
# The named object exists but is owned by a security descriptor this process
# cannot open - across an elevation or user boundary. That is not "we learned
# nothing", it is positive evidence that a run is in progress, so it refuses.
ERROR_ACCESS_DENIED = 5

Creator = Callable[[str], tuple[int, int]]

# One library object, with every declaration beside it. ctypes caches function
# pointers per library, so argtypes set on `windll.kernel32` would not apply to a
# separately constructed WinDLL - which is how a missing argtype goes unnoticed.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
# Without this, ctypes marshals a Python int as a 32-bit c_int and truncates any
# handle above 2^31. Small kernel handles hide it for years.
_kernel32.CloseHandle.restype = ctypes.c_bool
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


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
        _close(self.handle)
        self.handle = None


# "No lock was wanted" and "a lock could not be taken" both proceed, but they are
# not the same statement, and a bare RunLock() at a call site does not say which.
def not_taken() -> RunLock:
    return RunLock()


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
    if handle:
        return RunLock(handle=handle)
    # Only once the call has actually FAILED does the error code get to speak.
    # GetLastError is not guaranteed to be cleared on success, so reading it
    # beside a live handle could let a stale code from some earlier API invent a
    # busy lock and refuse a restart that nothing was blocking.
    return RunLock(busy=(error == ERROR_ACCESS_DENIED))


# The handle is kept for the life of the run: the mutex object exists for as long
# as any process holds one, which is what makes its mere existence mean "a run is
# in progress".
def _create_mutex(name: str) -> tuple[int, int]:
    handle = _kernel32.CreateMutexW(None, False, name)
    # use_last_error stashes the thread's GetLastError before anything else can
    # clobber it, so reading it here is reading the CreateMutexW call.
    return handle, ctypes.get_last_error()


# A handle returned alongside ERROR_ALREADY_EXISTS is a valid second handle to
# the same mutex, and leaking it would keep the object alive past our exit -
# making every later run look busy.
def _close(handle: int | None) -> None:
    if not handle:
        return
    try:
        _kernel32.CloseHandle(handle)
    except Exception:
        pass
