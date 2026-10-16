"""Durable state for the case approval workflow.

``SENTINEL_STATE_DIR`` names a directory (a volume in production) holding one JSON file. Writes are atomic
(temporary file in the same directory, fsync, replace, directory fsync) and the file is private (0600). When the
variable is unset the state is in-memory only. When it is set but the file cannot be read or is malformed,
:class:`StateUnavailable` is raised and callers must refuse to decide or execute, so a restart can never replay
an executed action or revive a rejected one."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile

FILENAME = "cases-state.json"
_KEYS = ("actions", "decisions", "receipts", "cases")


class StateUnavailable(RuntimeError):
    """The configured state file cannot be read, is malformed, or cannot be written."""


def directory() -> "str | None":
    return os.environ.get("SENTINEL_STATE_DIR") or None


def empty() -> dict:
    return {"version": 1, **{k: {} for k in _KEYS}}


def load() -> "dict | None":
    """The stored state, an empty state when no file exists yet, or None when persistence is not configured."""
    d = directory()
    if not d:
        return None
    try:
        with open(os.path.join(d, FILENAME), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as e:
        raise StateUnavailable(f"state file unreadable or malformed ({type(e).__name__})") from None
    if not (isinstance(data, dict) and data.get("version") == 1 and all(isinstance(data.get(k), dict) for k in _KEYS)):
        raise StateUnavailable("state file has an unexpected shape")
    return data


def save(data: dict) -> None:
    d = directory()
    if not d:
        return
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=FILENAME + ".", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(d, FILENAME))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:
        raise StateUnavailable(f"cannot write state file ({e.strerror or type(e).__name__})") from None


def health() -> dict:
    """Probe for startup and /health: is the configured state directory usable?"""
    d = directory()
    if not d:
        return {"ok": True, "persistent": False}
    try:
        load()
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".probe.")
        os.close(fd)
        os.unlink(tmp)
    except (OSError, StateUnavailable) as e:
        return {"ok": False, "persistent": True, "error": str(getattr(e, "strerror", None) or e)}
    return {"ok": True, "persistent": True}
