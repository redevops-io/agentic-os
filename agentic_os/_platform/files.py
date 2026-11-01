"""Private files, private directories and atomic replacement (standard library only).

POSIX behaviour is the code the permission and secret stores already had (mode bits, uids, ``0700`` directories) moved
here unchanged; macOS uses it as is. Windows has no mode bits, so there the same questions are answered from the
file's DACL and its location, and the answer is fail-closed:

* ``private_file_problem(path)`` returns a reason, or None when the file is private. On Windows it is private only when
  the file is a regular file (no link or reparse point), lives under the current user's profile directory, and its
  owner and DACL show no access for anyone but the user (the system and the machine's administrators, who can read the
  file anyway, are not counted). Any failure to read the descriptor is a refusal.
* ``ensure_private_dir(path)`` creates missing directories ``0700`` on POSIX and, on Windows, with a protected DACL
  that grants only the user and the system (inherited by the files created inside).
* ``atomic_write(path, data)`` is write-to-temp, fsync, replace, fsync the directory; ``replace_file`` retries the
  rename on Windows sharing violations (a reader that still has the old file open).
"""
from __future__ import annotations

import os
import stat
import tempfile
import time

from . import _acl

_WINDOWS = os.name == "nt"
if _WINDOWS:
    from . import _win32

#: True where permissions are POSIX mode bits and uids (Linux, macOS); False on Windows.
HAS_POSIX_MODES = not _WINDOWS

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
# Windows error codes for "the file is open elsewhere without delete sharing", which pass once the reader is done.
_SHARING = (5, 32, 33)
_REPLACE_ATTEMPTS = 20
_REPLACE_DELAY_S = 0.05


def _same_or_below(path: str, root: str) -> bool:
    p, r = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:                              # different drives
        return False


def private_file_problem(path) -> "str | None":
    """Why the existing file ``path`` is too exposed to hold a key or a secret, or None when it is private.

    POSIX: wider than 0600 (the owner's execute bit counts), or not owned by the running user (the mode of the file
    a symlink points at is what counts). Windows: see the module docstring. Raises ``OSError`` when the file cannot be
    examined, like ``os.stat``; callers that treat an unreadable file separately keep doing so."""
    if not _WINDOWS:
        st = os.stat(path)
        why = []
        if st.st_mode & 0o177:
            why.append(f"has mode {stat.S_IMODE(st.st_mode):04o}, wider than 0600")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            why.append("is not owned by the running user")
        return " and ".join(why) or None
    p = os.fspath(path)
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE:
        return "is a link or reparse point"
    try:
        profile = _win32.user_profile_dir()
        if not _same_or_below(p, profile):
            return f"is not under the user's profile directory ({profile})"
        return _acl.dacl_problem(_win32.owner_and_dacl_sddl(os.path.abspath(p)), _win32.current_user_sid())
    except OSError as e:
        return f"could not be shown to be private to the running user ({type(e).__name__}: {e})"


def make_private(path, *, is_dir: bool = False) -> None:
    """Make an existing file or directory private: ``0600``/``0700`` on POSIX, a protected user-and-system-only DACL on
    Windows. Raises ``OSError`` on failure (a caller that cannot make it private must not use it)."""
    if _WINDOWS:
        _win32.set_private_dacl(os.fspath(path), is_dir=is_dir)
    else:
        os.chmod(path, 0o700 if is_dir else 0o600)


def ensure_private_dir(path) -> None:
    """Create ``path`` and any MISSING parents private (``os.makedirs(mode=)`` would leave the intermediate ones at the
    default). Directories that already exist are left exactly as they are."""
    path = os.path.abspath(os.fspath(path))
    missing = []
    while path and not os.path.isdir(path):
        missing.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            continue
        if _WINDOWS:
            _win32.set_private_dacl(d, is_dir=True)


def fsync_dir(path) -> None:
    """Flush a directory's entries to disk. A no-op where the platform cannot open a directory (Windows)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace_file(src, dst) -> None:
    """``os.replace`` that, on Windows, retries for about a second when a reader still holds the destination open
    (a sharing violation). On POSIX it is exactly ``os.replace``."""
    if not _WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            if getattr(e, "winerror", None) not in _SHARING or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_S)


def atomic_write(path, data: bytes) -> None:
    """Replace ``path`` with ``data`` all-or-nothing: a temp file in the same directory (private, created exclusively),
    write and fsync, ``replace_file``, fsync the directory. A failure or a crash leaves the previous file or the
    complete new one, never a partial one; the temp file is removed on failure."""
    path = os.fspath(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        make_private(tmp)
        replace_file(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(d)
