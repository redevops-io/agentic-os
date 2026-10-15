"""Portable inter-process lock: an exclusive lock FILE created with ``O_CREAT|O_EXCL``.

No ``fcntl`` (POSIX-only) and no ``msvcrt`` (Windows-only): ``os.open(..., O_EXCL)`` is atomic on Linux, macOS and
Windows (local filesystems). A lock older than ``stale_s`` is treated as abandoned (its owner crashed) and is broken
by an atomic rename, so exactly one waiter wins the break. Not a network-filesystem lock; documented in the README.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from .errors import Conflict


class FileLock:
    def __init__(self, path: Path, *, timeout_s: float = 10.0, stale_s: float = 60.0, poll_s: float = 0.01):
        self.path = Path(path)
        self.timeout_s, self.stale_s, self.poll_s = timeout_s, stale_s, poll_s
        self._token = uuid.uuid4().hex
        self._held = False

    def _try_create(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except PermissionError:
            # Windows reports a lock file that is mid-delete as PermissionError: treat as "held" and retry
            return False
        try:
            os.write(fd, self._token.encode("ascii"))
        finally:
            os.close(fd)
        return True

    def _break_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age <= self.stale_s:
            return
        grave = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".stale")
        try:
            os.replace(str(self.path), str(grave))       # atomic: only one breaker succeeds
        except OSError:
            return
        try:
            os.unlink(str(grave))
        except OSError:
            pass

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            if self._try_create():
                self._held = True
                return
            self._break_if_stale()
            if time.monotonic() > deadline:
                raise Conflict(f"could not acquire lock {self.path.name} within {self.timeout_s}s")
            time.sleep(self.poll_s)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if self.path.read_text(encoding="ascii") == self._token:      # never delete someone else's lock
                os.unlink(str(self.path))
        except OSError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: temp file in the same directory, fsync, ``os.replace``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(40):               # Windows: replacing a file another handle has open raises PermissionError
            try:
                os.replace(str(tmp), str(path))
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.025)
    finally:
        try:
            if tmp.exists():
                os.unlink(str(tmp))
        except OSError:
            pass
