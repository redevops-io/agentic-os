"""Inter-process exclusive file locks (standard library only): ``flock`` on POSIX, ``LockFileEx`` on Windows.

One small contract for every writer that must not interleave with another process (the grant store transaction,
the hash-chained mission event log, the outcome store, the fork/join driver claim):

* ``acquire(handle, timeout)`` / ``release(handle)`` lock an already-open file (an ``int`` descriptor or a file object);
* ``locked(handle, timeout)`` is the context manager around the two;
* ``file_lock(path, timeout=...)`` opens (creating) a lock file and holds the lock for the block.

A platform that has neither mechanism raises ``LockUnavailable``: callers fail closed, they never carry on unlocked.
On POSIX with no timeout the lock is the blocking ``flock`` the callers used before, so behaviour there is unchanged;
with a timeout the wait is a bounded poll that ends in ``LockTimeout``. On Windows the lock is advisory in effect:
it covers one byte far beyond the data, so it never blocks a reader or writer of the file's content.
"""
from __future__ import annotations

import contextlib
import errno
import os
import time
from typing import Iterator, Union

try:
    import fcntl as _fcntl
except ImportError:  # not POSIX
    _fcntl = None

_WINDOWS = os.name == "nt"
if _WINDOWS:
    from . import _win32

_POLL_S = 0.02
_BUSY = (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)

Handle = Union[int, "os.PathLike", object]


class LockUnavailable(OSError):
    """This platform has no inter-process file lock the layer can use."""


class LockTimeout(TimeoutError):
    """The lock stayed held by another process for the whole wait."""


def available() -> bool:
    return _fcntl is not None or _WINDOWS


def _fd(handle) -> int:
    return handle if isinstance(handle, int) else handle.fileno()


def acquire(handle, timeout: "float | None" = None) -> None:
    """Take the exclusive lock on an open file. ``timeout`` None waits without bound; a number is the longest wait in
    seconds (0 tries once) and a miss raises ``LockTimeout``. Any other failure is an ``OSError``."""
    fd = _fd(handle)
    if _fcntl is not None:
        if timeout is None:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                return
            except OSError as e:
                if e.errno not in _BUSY:
                    raise
            if time.monotonic() >= deadline:
                raise LockTimeout(f"the lock stayed held for {timeout:g} s")
            time.sleep(_POLL_S)
    elif _WINDOWS:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not _win32.try_lock(fd):
            if deadline is not None and time.monotonic() >= deadline:
                raise LockTimeout(f"the lock stayed held for {timeout:g} s")
            time.sleep(_POLL_S)
    else:
        raise LockUnavailable("this platform has neither flock nor LockFileEx: refusing to run without a lock")


def release(handle) -> None:
    """Release a lock taken with ``acquire``. Closing the file releases it too; this makes the release immediate, which
    Windows does not promise for a bare close."""
    fd = _fd(handle)
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    elif _WINDOWS:
        try:
            _win32.unlock(fd)
        except OSError:
            pass                                   # not held (or the handle is closing): nothing left to release


@contextlib.contextmanager
def locked(handle, timeout: "float | None" = None) -> Iterator[None]:
    acquire(handle, timeout)
    try:
        yield
    finally:
        release(handle)


@contextlib.contextmanager
def file_lock(path, *, timeout: "float | None" = None, mode: int = 0o600, no_follow: bool = False) -> Iterator[int]:
    """Hold the exclusive lock on the file at ``path`` (created if missing, with ``mode`` on POSIX) for the block; yields
    the descriptor. ``no_follow`` refuses a symbolic link where the platform supports it (POSIX ``O_NOFOLLOW``)."""
    flags = os.O_RDWR | os.O_CREAT | (getattr(os, "O_NOFOLLOW", 0) if no_follow else 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, mode)
    try:
        acquire(fd, timeout)
        try:
            yield fd
        finally:
            release(fd)
    finally:
        os.close(fd)
