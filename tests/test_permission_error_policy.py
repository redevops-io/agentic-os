"""Which `PermissionError` the grant store turns into its own error, and which it leaves alone.

Only a Windows SHARING VIOLATION (winerror 5, 32 or 33, on Windows) that outlasts the bounded replace retry becomes `GrantStorageBusy`, which is
both a `GrantStorageError` (the admin routes translate it) and an `OSError` (so the plaintext-migration tolerance, which swallows an `OSError` from
the rollback-counter write exactly as before, still applies). Every other `PermissionError` (a directory the process may not write to on Linux or
macOS, a Windows privilege error) propagates unchanged."""
from __future__ import annotations

import base64
import os

import pytest

from agentic_os import permissions as perm
from agentic_os._platform import files
from agentic_os.permissions import GrantStorageError, GrantStore

KEY = base64.b64encode(b"k" * 32).decode()


def _perm_error(winerror=None):
    e = PermissionError(13, "denied")
    if winerror is not None:
        e.winerror = winerror
    return e


@pytest.fixture
def as_windows(monkeypatch):
    """Make the files layer behave as on Windows for the retry/wrap decision (the Windows API itself is not needed)."""
    monkeypatch.setattr(files, "_WINDOWS", True)
    monkeypatch.setattr(files, "_REPLACE_DELAY_S", 0)
    monkeypatch.setattr(files, "create_private_temp", lambda d, pre, suf: __import__("tempfile").mkstemp(dir=d, prefix=pre, suffix=suf))
    monkeypatch.setattr(files, "ensure_private_dir", lambda p: os.makedirs(p, mode=0o700, exist_ok=True))
    monkeypatch.setattr(files, "create_exclusive", lambda p, mode=0o600: os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode))


def _fail_replace(monkeypatch, err, only="grants.counter"):
    real = os.replace

    def replace(src, dst, *a, **k):
        if str(dst).endswith(only):
            raise err
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", replace)


def test_a_permission_error_without_a_windows_code_propagates_unchanged(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_bytes(b"old")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(_perm_error()))
    with pytest.raises(PermissionError) as e:
        perm._atomic_write(p, b"new")
    assert not isinstance(e.value, GrantStorageError) and p.read_bytes() == b"old"


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs a POSIX directory the process may not write to")
def test_a_directory_that_cannot_be_written_keeps_raising_permission_error(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(d, 0o500)
    try:
        with pytest.raises(PermissionError) as e:
            perm._atomic_write(d / "f", b"x")
        assert not isinstance(e.value, GrantStorageError)
    finally:
        os.chmod(d, 0o700)


@pytest.mark.parametrize("winerror,wrapped", [(5, True), (32, True), (33, True), (1314, False), (None, False)])
def test_only_windows_sharing_violations_are_wrapped(tmp_path, monkeypatch, as_windows, winerror, wrapped):
    p = tmp_path / "f"
    p.write_bytes(b"old")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(_perm_error(winerror)))
    if wrapped:
        with pytest.raises(GrantStorageError, match="in use by another process") as e:
            perm._atomic_write(p, b"new")
        assert isinstance(e.value, perm.GrantStorageBusy) and isinstance(e.value, OSError)
    else:
        with pytest.raises(PermissionError) as e:
            perm._atomic_write(p, b"new")
        assert not isinstance(e.value, GrantStorageError)
    assert p.read_bytes() == b"old"


def test_a_sharing_violation_wins_nothing_on_a_platform_that_is_not_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(_perm_error(32)))
    with pytest.raises(PermissionError) as e:
        perm._atomic_write(tmp_path / "f", b"new")
    assert not isinstance(e.value, GrantStorageError)


def _plaintext_store(tmp_path, monkeypatch):
    d = tmp_path / "g"
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    monkeypatch.delenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", raising=False)
    GrantStore(str(d), allow_plaintext_dev=True).add("app", "support", "table", "crm.customers")      # an old plaintext store
    assert (d / "grants.jsonl").exists()
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    return d


def test_the_migration_still_opens_the_store_when_the_counter_write_is_refused(tmp_path, monkeypatch):
    """Behaviour on `main`: a PermissionError from the first rollback-counter write is tolerated (the first real write records it)."""
    d = _plaintext_store(tmp_path, monkeypatch)
    _fail_replace(monkeypatch, _perm_error())
    s = GrantStore(str(d))
    assert s.migrated_from_plaintext == 1 and (d / "grants.enc").exists() and [g.subject for g in s.list()] == ["support"]


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_the_migration_tolerates_a_sharing_violation_on_the_counter_write_too(tmp_path, monkeypatch, as_windows, winerror):
    d = _plaintext_store(tmp_path, monkeypatch)
    _fail_replace(monkeypatch, _perm_error(winerror))
    s = GrantStore(str(d))
    assert s.migrated_from_plaintext == 1 and [g.subject for g in s.list()] == ["support"]


def test_a_symlinked_counter_is_still_refused_not_tolerated(tmp_path, monkeypatch):
    """The tolerance is for a failing write, never for the symlink refusal that keeps the rollback floor honest."""
    if not hasattr(os, "symlink") or os.name == "nt":
        pytest.skip("needs an unprivileged symlink")
    d = _plaintext_store(tmp_path, monkeypatch)
    if (d / "grants.counter").exists():
        (d / "grants.counter").unlink()
    os.symlink(tmp_path / "elsewhere", d / "grants.counter")
    with pytest.raises(GrantStorageError, match="symbolic link"):
        GrantStore(str(d))
