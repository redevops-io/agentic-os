"""Windows API calls through ``ctypes`` (standard library only). Imported on Windows only.

Three small groups, each used by one function of the portability layer:

* byte-range file locks (``LockFileEx``), behind ``locks``;
* the owner and DACL of a file (``GetNamedSecurityInfoW`` and friends), behind ``files.private_file_problem`` and
  ``files.make_private``;
* a process liveness probe that never signals the process, behind ``process.pid_alive``.

Every function raises ``OSError`` (a ``WinError``) when a call fails; the callers treat that as "cannot show it is
safe" and refuse.
"""
from __future__ import annotations

import ctypes
import msvcrt
import os
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_userenv = ctypes.WinDLL("userenv", use_last_error=True)

_PVOID = ctypes.c_void_p
_PPVOID = ctypes.POINTER(ctypes.c_void_p)

# ── prototypes (explicit, so 64-bit handles and pointers are never truncated) ──
_k32.LocalFree.argtypes = [_PVOID]
_k32.LocalFree.restype = _PVOID
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", _PVOID), ("bInheritHandle", wintypes.BOOL)]


_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SECURITY_ATTRIBUTES)]
_k32.CreateDirectoryW.restype = wintypes.BOOL


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


_k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(_OVERLAPPED)]
_k32.LockFileEx.restype = wintypes.BOOL
_k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(_OVERLAPPED)]
_k32.UnlockFileEx.restype = wintypes.BOOL

_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, _PVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.ConvertSidToStringSidW.argtypes = [_PVOID, ctypes.POINTER(wintypes.LPWSTR)]
_adv.ConvertSidToStringSidW.restype = wintypes.BOOL
_adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, _PPVOID]
_adv.ConvertStringSidToSidW.restype = wintypes.BOOL
_adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, _PPVOID, _PPVOID, _PPVOID, _PPVOID, _PPVOID]
_adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
_adv.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, _PVOID, _PVOID, _PVOID, _PVOID]
_adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [_PVOID, wintypes.DWORD, wintypes.DWORD,
                                                                      ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, _PPVOID,
                                                                      ctypes.POINTER(wintypes.ULONG)]
_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_adv.GetSecurityDescriptorDacl.argtypes = [_PVOID, ctypes.POINTER(wintypes.BOOL), _PPVOID, ctypes.POINTER(wintypes.BOOL)]
_adv.GetSecurityDescriptorDacl.restype = wintypes.BOOL
_userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL

_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2
_ERROR_LOCK_VIOLATION = 33
_ERROR_INVALID_PARAMETER = 87
_ERROR_INSUFFICIENT_BUFFER = 122
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_TOKEN_QUERY = 0x8
_TOKEN_USER = 1
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1

# The locked byte sits far beyond any record the files hold (1 TiB), so a mandatory Windows byte-range lock never
# blocks a reader or writer of the data itself (a POSIX flock is advisory and whole-file).
_LOCK_OFFSET = 1 << 40
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_FILE_SHARE_READ_WRITE = 0x3                  # what Python's own os.open uses (no delete sharing)
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x80


def _overlapped() -> _OVERLAPPED:
    ov = _OVERLAPPED()
    ov.Offset = _LOCK_OFFSET & 0xFFFFFFFF
    ov.OffsetHigh = _LOCK_OFFSET >> 32
    return ov


# ── locks ────────────────────────────────────────────────────────────────────────────────────────────────

def try_lock(fd: int) -> bool:
    """Take the exclusive lock on the open file ``fd`` without waiting: True when taken, False when another handle holds it."""
    ov = _overlapped()
    if _k32.LockFileEx(msvcrt.get_osfhandle(fd), _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, ctypes.byref(ov)):
        return True
    err = ctypes.get_last_error()
    if err == _ERROR_LOCK_VIOLATION:
        return False
    raise ctypes.WinError(err)


def unlock(fd: int) -> None:
    ov = _overlapped()
    if not _k32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(ov)):
        raise ctypes.WinError(ctypes.get_last_error())


# ── processes ────────────────────────────────────────────────────────────────────────────────────────────

def pid_alive(pid: int) -> bool:
    """Is a process with this id running? Opens it for query and synchronise only and waits zero time; nothing is ever sent
    to it (``os.kill(pid, 0)`` would TERMINATE the process on Windows). When it cannot be told, the answer is True, like
    the POSIX probe's answer for a process it may not signal."""
    if pid <= 0:
        return False
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not h:
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        r = _k32.WaitForSingleObject(h, 0)
        return r != _WAIT_OBJECT_0
    finally:
        _k32.CloseHandle(h)


# ── security descriptors ─────────────────────────────────────────────────────────────────────────────────

def _local_free(p) -> None:
    if p:
        _k32.LocalFree(p)


def _wstr_and_free(p: wintypes.LPWSTR) -> str:
    value = p.value or ""
    _local_free(ctypes.cast(p, _PVOID))
    return value


def _token() -> wintypes.HANDLE:
    tok = wintypes.HANDLE()
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(tok)):
        raise ctypes.WinError(ctypes.get_last_error())
    return tok


def current_user_sid() -> str:
    """The SID string (``S-1-5-21-...``) of the user this process runs as."""
    tok = _token()
    try:
        n = wintypes.DWORD(0)
        _adv.GetTokenInformation(tok, _TOKEN_USER, None, 0, ctypes.byref(n))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not n.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(n.value)
        if not _adv.GetTokenInformation(tok, _TOKEN_USER, buf, n, ctypes.byref(n)):
            raise ctypes.WinError(ctypes.get_last_error())
        psid = ctypes.cast(buf, _PPVOID)[0]            # TOKEN_USER begins with the SID pointer
        out = wintypes.LPWSTR()
        if not _adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _wstr_and_free(out)
    finally:
        _k32.CloseHandle(tok)


def resolve_sid(token: str) -> str:
    """An SDDL trustee abbreviation (``LA``, ``DA``, ...) or SID string as a full SID string."""
    psid = _PVOID()
    if not _adv.ConvertStringSidToSidW(token, ctypes.byref(psid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        out = wintypes.LPWSTR()
        if not _adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _wstr_and_free(out)
    finally:
        _local_free(psid)


def user_profile_dir() -> str:
    """The profile directory Windows records for the current user (not read from an environment variable)."""
    tok = _token()
    try:
        n = wintypes.DWORD(0)
        _userenv.GetUserProfileDirectoryW(tok, None, ctypes.byref(n))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not n.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_unicode_buffer(n.value)
        if not _userenv.GetUserProfileDirectoryW(tok, buf, ctypes.byref(n)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.value
    finally:
        _k32.CloseHandle(tok)


def owner_and_dacl_sddl(path: str) -> str:
    """The owner and DACL of ``path`` as an SDDL string, e.g. ``O:BAD:PAI(A;;FA;;;SY)(A;;FA;;;S-1-5-21-...)``."""
    psd = _PVOID()
    rc = _adv.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                                    None, None, None, None, ctypes.byref(psd))
    if rc:
        raise ctypes.WinError(rc)
    try:
        out = wintypes.LPWSTR()
        if not _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                psd, _SDDL_REVISION_1, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION, ctypes.byref(out), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return _wstr_and_free(out)
    finally:
        _local_free(psd)


def set_private_dacl(path: str, *, is_dir: bool) -> None:
    """Replace ``path``'s DACL with a PROTECTED one (no inherited entries) that grants full control to the current user
    and the system only. For a directory the entries are inherited by what is created inside it."""
    user = current_user_sid()
    inherit = "OICI" if is_dir else ""
    sddl = f"D:P(A;{inherit};FA;;;{user})(A;{inherit};FA;;;SY)"
    psd = _PVOID()
    if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, _SDDL_REVISION_1, ctypes.byref(psd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), _PVOID()
        if not _adv.GetSecurityDescriptorDacl(psd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
            raise ctypes.WinError(ctypes.get_last_error())
        rc = _adv.SetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                                        None, None, dacl, None)
        if rc:
            raise ctypes.WinError(rc)
    finally:
        _local_free(psd)


def _private_sa(*, is_dir: bool):
    """A SECURITY_ATTRIBUTES whose descriptor carries a PROTECTED DACL for the current user and the system only, so an object created
    with it is private from its first instant (nothing is inherited from the parent). Returns ``(sa, psd)``; free ``psd`` with ``_local_free``."""
    user = current_user_sid()
    inherit = "OICI" if is_dir else ""
    sddl = f"D:P(A;{inherit};FA;;;{user})(A;{inherit};FA;;;SY)"
    psd = _PVOID()
    if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, _SDDL_REVISION_1, ctypes.byref(psd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), psd, False), psd


def create_private_file(path: str) -> int:
    """Create a NEW file that is private (user and system only) from creation and return an ``os``-level descriptor for it (binary,
    read/write). ``FileExistsError`` when the path exists; any other failure is an ``OSError`` and nothing is left behind."""
    sa, psd = _private_sa(is_dir=False)
    try:
        h = _k32.CreateFileW(path, _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ_WRITE, ctypes.byref(sa), _CREATE_NEW,
                             _FILE_ATTRIBUTE_NORMAL, None)
        if h is None or h == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            if err in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                raise FileExistsError(17, "already exists", path)
            raise ctypes.WinError(err)
    finally:
        _local_free(psd)
    try:
        return msvcrt.open_osfhandle(h, os.O_RDWR | os.O_BINARY)
    except BaseException:
        _k32.CloseHandle(h)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def create_private_dir(path: str) -> None:
    """Create a NEW directory that is private (user and system only, inherited by what is created in it) from creation."""
    sa, psd = _private_sa(is_dir=True)
    try:
        if not _k32.CreateDirectoryW(path, ctypes.byref(sa)):
            err = ctypes.get_last_error()
            if err in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                raise FileExistsError(17, "already exists", path)
            raise ctypes.WinError(err)
    finally:
        _local_free(psd)
