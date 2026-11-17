"""Process liveness that never signals the process (standard library only)."""
from __future__ import annotations

import os

_WINDOWS = os.name == "nt"
if _WINDOWS:
    from . import _win32


def pid_alive(pid: int) -> bool:
    """Is a process with this id running? On POSIX the ``kill(pid, 0)`` probe (permission denied counts as alive). On
    Windows ``os.kill`` with any signal but the console events TERMINATES the process, so it is never used there: the
    process is opened for query and waited on for zero time instead. When it cannot be told, the answer is True."""
    if _WINDOWS:
        return _win32.pid_alive(int(pid))
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True
