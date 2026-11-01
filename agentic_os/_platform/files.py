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

import errno
import os
import secrets
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
_READ_ATTEMPTS = 40
_READ_DELAY_S = 0.025


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
        return _acl.dacl_problem(_win32.owner_and_dacl_sddl(os.path.abspath(p)), _win32.current_user_sid(), _win32.resolve_sid)
    except OSError as e:
        return f"could not be shown to be private to the running user ({type(e).__name__}: {e})"


def read_regular(path) -> bytes:
    """Read a regular file without following a symbolic link. POSIX: ``O_NOFOLLOW``. Windows: a link or reparse point is refused
    first, and a ``PermissionError`` is retried for about a second, because a lock-free reader that meets a concurrent
    atomic replace (``replace_file``) can have its open refused for a moment; without the retry it would mistake the replace for a
    failure."""
    if _WINDOWS:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE:
            raise OSError(errno.ELOOP, "refusing to read through a link", os.fspath(path))
        for attempt in range(_READ_ATTEMPTS):
            try:
                with open(path, "rb") as f:
                    return f.read()
            except PermissionError:
                if attempt == _READ_ATTEMPTS - 1:
                    raise
                time.sleep(_READ_DELAY_S)
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        return f.read()


def create_exclusive(path, mode: int = 0o600) -> int:
    """Create a NEW file and return its descriptor: never an existing path, never through a symbolic link. POSIX: ``O_EXCL`` with
    ``O_NOFOLLOW``. Windows ignores ``mode`` and would create through a dangling link, so an existing path of any kind (a link
    included) is refused first, and the file is created with a protected user-and-system-only DACL, so it is private from its first
    instant. Raises ``FileExistsError`` for an existing path."""
    if _WINDOWS:
        if os.path.lexists(path):
            raise FileExistsError(errno.EEXIST, "already exists", os.fspath(path))
        return _win32.create_private_file(os.fspath(path))    # private from creation (a DACL set afterwards would leave a window)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | getattr(os, "O_BINARY", 0), mode)


def touch_private(path) -> None:
    """Windows only (a no-op elsewhere, where the file keeps the mode its creator gives it): make sure ``path`` exists and was created private,
    so a log or lock file that a later ``open`` would create under the parent's ACL is never created exposed. An existing file is left as it is."""
    if not _WINDOWS or os.path.lexists(path):
        return
    try:
        os.close(_win32.create_private_file(os.fspath(path)))
    except FileExistsError:
        pass


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
            if _WINDOWS:
                _win32.create_private_dir(d)                  # private from creation: nothing is inherited, nothing to harden afterwards
            else:
                os.mkdir(d, 0o700)
        except FileExistsError:
            continue


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


def is_sharing_violation(e: BaseException) -> bool:
    """Is ``e`` a Windows sharing violation (``winerror`` 5, 32 or 33, on Windows): the only ``PermissionError`` that passes once the other
    process lets go? Anything else (a POSIX permission problem, another Windows error) is not."""
    return _WINDOWS and isinstance(e, PermissionError) and getattr(e, "winerror", None) in _SHARING


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
            if not is_sharing_violation(e) or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_S)


def create_private_temp(directory, prefix: str, suffix: str) -> "tuple[int, str]":
    """A new temporary file in ``directory``, private from creation: ``mkstemp`` (0600) on POSIX, a file created with a protected
    user-and-system-only DACL on Windows (so no data is ever written to a file the parent's ACL exposes). Returns ``(fd, path)``."""
    if not _WINDOWS:
        return tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    for _ in range(100):
        path = os.path.join(os.fspath(directory), f"{prefix}{secrets.token_hex(4)}{suffix}")
        try:
            return create_exclusive(path), path
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "could not find an unused temporary name", os.fspath(directory))


def atomic_write(path, data: bytes) -> None:
    """Replace ``path`` with ``data`` all-or-nothing: a temp file in the same directory (private, created exclusively),
    write and fsync, ``replace_file``, fsync the directory. A failure or a crash leaves the previous file or the
    complete new one, never a partial one; the temp file is removed on failure."""
    path = os.fspath(path)
    d = os.path.dirname(path) or "."
    fd, tmp = create_private_temp(d, os.path.basename(path) + ".", ".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        replace_file(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(d)
