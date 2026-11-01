"""Private files, private directories, atomic replacement and the process probe (``agentic_os._platform``).

The same tests run on Linux, macOS and Windows. POSIX checks the mode bits and the owner; Windows has none, so it checks
where the file lives and who its DACL names, and refuses when it cannot tell. Tests of a property one platform cannot
express are marked for the other with the reason; the property each one proves is covered by the other platform's test."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_os._platform import _acl, files, process

WINDOWS = os.name == "nt"
USER = "S-1-5-21-1-2-3-1001"
OTHER = "S-1-5-21-1-2-3-1002"


# ── the descriptor rule, as pure logic (runs everywhere) ──

@pytest.mark.parametrize("sddl", [
    f"O:{USER}D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{USER})",
    f"O:BAD:P(A;OICI;FA;;;{USER})(A;OICI;FA;;;SY)",
    f"O:SYD:(A;;FA;;;{USER})",
    f"O:{USER}D:PAI(A;;FA;;;{USER})(D;;FR;;;WD)",                    # a DENY entry grants nothing
    f"O:{USER}D:PAI(A;;FA;;;{USER})(A;OICIIO;GR;;;WD)",              # inherit-only: does not apply to this object
    f"O:{USER}D:(A;;FA;;;CO)(A;;FA;;;{USER})",
])
def test_a_descriptor_for_one_user_is_private(sddl):
    assert _acl.dacl_problem(sddl, USER) is None


@pytest.mark.parametrize("sddl,who", [
    (f"O:{USER}D:PAI(A;;FA;;;{USER})(A;;FR;;;WD)", "S-1-1-0"),
    (f"O:{USER}D:PAI(A;;FA;;;{USER})(A;;FR;;;BU)", "S-1-5-32-545"),
    (f"O:{USER}D:PAI(A;;FA;;;{USER})(A;;FR;;;AU)", "S-1-5-11"),
    (f"O:{USER}D:PAI(A;;FA;;;{OTHER})", OTHER),
    (f"O:{USER}D:PAI(A;;FA;;;{USER})(XA;;FA;;;WD;(Member_of{{SID(WD)}}))", "S-1-1-0"),
    # a conditional ACE (nested parentheses) must not hide the Everyone ACE after it
    (f"O:{USER}D:PAI(XA;;FA;;;{USER};(Member_of{{SID(BA)}}))(A;;FR;;;WD)", "S-1-1-0"),
    (f"O:{USER}G:SYD:PAI(A;;FA;;;{USER})(A;;FR;;;WD)S:(AU;SA;FA;;;WD)", "S-1-1-0"),
])
def test_any_other_principal_is_exposure(sddl, who):
    why = _acl.dacl_problem(sddl, USER)
    assert why and who in why


def test_an_abbreviation_that_is_the_running_user_is_resolved_not_mistaken_for_a_stranger():
    """SDDL writes the machine's built-in Administrator as ``LA``; when that account is the running user (a CI runner), the abbreviation
    must resolve to the user's SID. Without a resolver an unknown abbreviation stays foreign (fail closed)."""
    sddl = f"O:LAD:PAI(A;;FA;;;LA)(A;;FA;;;SY)"
    assert "LA" in _acl.dacl_problem(sddl, USER)
    assert _acl.dacl_problem(sddl, USER, resolve=lambda t: USER if t == "LA" else t) is None

    def broken(token):
        raise OSError("no such account")
    assert _acl.dacl_problem(sddl, USER, resolve=broken)


def test_a_missing_dacl_or_a_foreign_owner_is_refused():
    assert "no DACL" in _acl.dacl_problem(f"O:{USER}G:SY", USER)
    assert "owned by" in _acl.dacl_problem(f"O:{OTHER}D:PAI(A;;FA;;;{USER})", USER)
    assert _acl.dacl_problem("garbage", USER)
    assert "parsed" in _acl.dacl_problem(f"O:{USER}D:PAI(A;;FA;;;{USER}", USER)      # unbalanced: refuse, never guess


@pytest.mark.parametrize("sddl,why", [
    (f"O:{USER}D:NO_ACCESS_CONTROL", "NULL"),                                   # a NULL DACL: every account has full access
    (f"O:{USER}D:PNO_ACCESS_CONTROL", "NULL"),
    (f"O:{USER}D:PAI(A;;FA;;WD)", "malformed"),                                 # a 5-field ACE must be refused, not skipped
    (f"O:{USER}D:PAI(A;;FA;;;{USER})(A;;FA;;WD)", "malformed"),
    (f"O:{USER}D:PAI(Q;;FA;;;{USER})", "unknown"),                              # an ACE type the rule does not know
    (f"O:{USER}D:PAI(A;;FA;;;{USER})garbage", "parsed"),
    (f"O:{USER}D:", "empty"),                                                   # nothing is granted: refuse rather than guess
])
def test_a_null_empty_or_malformed_dacl_is_refused_not_accepted(sddl, why):
    got = _acl.dacl_problem(sddl, USER)
    assert got and why in got, got


def test_a_permission_error_after_the_bounded_retry_is_a_grant_storage_error(tmp_path, monkeypatch):
    """A destination held open past the retry window (an antivirus scan, a stuck reader) must surface as the store's own error, which the
    admin routes translate, not as a raw PermissionError (a 500)."""
    from agentic_os import permissions as perm
    p = tmp_path / "f"
    p.write_bytes(b"old")

    def held(src, dst):
        e = PermissionError(13, "in use")
        e.winerror = 32
        raise e
    monkeypatch.setattr(files, "_WINDOWS", True)
    monkeypatch.setattr(files, "_REPLACE_DELAY_S", 0)
    monkeypatch.setattr(files, "make_private", lambda *a, **k: None)           # the Windows API is not there to call on this host
    monkeypatch.setattr(os, "replace", held)
    with pytest.raises(perm.GrantStorageError, match="in use by another process"):
        perm._atomic_write(p, b"new")
    assert p.read_bytes() == b"old" and sorted(x.name for x in tmp_path.iterdir()) == ["f"]



# ── private directories and files ──

def test_ensure_private_dir_creates_missing_parents_private_and_leaves_existing_ones(tmp_path):
    existing = tmp_path / "keep"
    existing.mkdir()
    if not WINDOWS:
        os.chmod(existing, 0o755)
    target = existing / "a" / "b"
    files.ensure_private_dir(target)
    assert target.is_dir()
    if WINDOWS:
        assert files.private_file_problem(target) is None
        assert files.private_file_problem(existing / "a") is None
    else:
        assert (os.stat(target).st_mode & 0o777) == 0o700 and (os.stat(existing / "a").st_mode & 0o777) == 0o700
        assert (os.stat(existing).st_mode & 0o777) == 0o755                # an existing directory is not touched
    files.ensure_private_dir(target)                                         # idempotent


def test_atomic_write_is_exact_replaces_in_full_and_leaves_no_temp_file(tmp_path):
    d = tmp_path / "d"
    files.ensure_private_dir(d)
    p = d / "f.bin"
    files.atomic_write(p, b"one\nline\x00\xff")
    assert p.read_bytes() == b"one\nline\x00\xff"                           # bytes, no newline translation on any OS
    files.atomic_write(p, b"two")
    assert p.read_bytes() == b"two"
    assert sorted(x.name for x in d.iterdir()) == ["f.bin"]
    assert files.private_file_problem(p) is None                             # what it writes is private on every platform


def test_a_failed_write_keeps_the_previous_file_and_removes_the_temp_file(tmp_path, monkeypatch):
    p = tmp_path / "f"
    files.atomic_write(p, b"old")
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(files, "replace_file", boom)
    with pytest.raises(OSError):
        files.atomic_write(p, b"new")
    assert p.read_bytes() == b"old" and sorted(x.name for x in tmp_path.iterdir()) == ["f"]


def test_replace_waits_for_a_reader_that_still_holds_the_old_file(tmp_path):
    """On Windows a replace over a file a lock-free reader has open is a sharing violation; the retry waits the reader out.
    On POSIX it never fails: the test then proves the replace is unaffected by an open reader."""
    p = tmp_path / "f"
    p.write_bytes(b"old")
    reader = open(p, "rb")
    threading.Timer(0.3, reader.close).start()
    files.atomic_write(p, b"new")
    reader.close()
    assert p.read_bytes() == b"new"


def test_replace_retries_only_sharing_violations_and_only_on_windows(tmp_path, monkeypatch):
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            e = PermissionError(13, "in use")
            e.winerror = 32
            raise e
    monkeypatch.setattr(files, "_WINDOWS", True)
    monkeypatch.setattr(files, "_REPLACE_DELAY_S", 0)
    monkeypatch.setattr(os, "replace", flaky)
    files.replace_file("a", "b")
    assert len(calls) == 3                                                   # retried twice, then succeeded

    calls.clear()
    def denied(src, dst):
        calls.append(1)
        e = PermissionError(13, "other")
        e.winerror = 1314                                                    # a privilege problem is not a sharing violation
        raise e
    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(PermissionError):
        files.replace_file("a", "b")
    assert len(calls) == 1
    monkeypatch.setattr(files, "_WINDOWS", False)                            # POSIX: exactly os.replace, one call
    calls.clear()
    with pytest.raises(PermissionError):
        files.replace_file("a", "b")
    assert len(calls) == 1


@pytest.mark.skipif(WINDOWS, reason="POSIX mode bits and uids; the Windows counterpart is the DACL tests below")
def test_posix_a_key_file_wider_than_0600_is_refused_and_0600_is_not(tmp_path):
    p = tmp_path / "k"
    p.write_text("x")
    os.chmod(p, 0o600)
    assert files.private_file_problem(p) is None
    os.chmod(p, 0o640)
    assert "0640" in files.private_file_problem(p)
    os.chmod(p, 0o700)                                                       # the owner's execute bit counts too
    assert files.private_file_problem(p)


@pytest.mark.skipif(WINDOWS, reason="uid ownership is a POSIX concept; Windows checks the DACL owner")
def test_posix_a_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "k"
    p.write_text("x")
    os.chmod(p, 0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(p).st_uid + 1)
    assert "not owned" in files.private_file_problem(p)


def _icacls(*args: str) -> None:
    subprocess.run(["icacls", *args], check=True, capture_output=True, text=True)


@pytest.mark.skipif(not WINDOWS, reason="DACLs are Windows; POSIX checks mode bits (test above)")
class TestWindowsPrivateFiles:
    def test_a_file_in_a_private_directory_is_private(self, tmp_path):
        d = tmp_path / "p"
        files.ensure_private_dir(d)
        f = d / "k"
        f.write_text("x")
        assert files.private_file_problem(f) is None

    def test_granting_everyone_access_makes_it_exposed_and_make_private_repairs_it(self, tmp_path):
        f = tmp_path / "k"
        f.write_text("x")
        _icacls(str(f), "/grant", "*S-1-1-0:R")                              # Everyone: read
        why = files.private_file_problem(f)
        assert why and "S-1-1-0" in why
        files.make_private(f)
        assert files.private_file_problem(f) is None

    def test_authenticated_users_and_users_are_exposure_too(self, tmp_path):
        for sid in ("*S-1-5-11", "*S-1-5-32-545"):                           # Authenticated Users, BUILTIN\Users
            f = tmp_path / f"k{sid[-3:]}"
            f.write_text("x")
            _icacls(str(f), "/grant", f"{sid}:R")
            assert files.private_file_problem(f), sid

    def test_a_file_outside_the_user_profile_is_refused_even_with_a_private_dacl(self, tmp_path):
        public = os.environ.get("PUBLIC")
        if not public or not os.access(public, os.W_OK):
            pytest.skip("no writable location outside the user profile on this machine")
        d = Path(public) / f"agentic-os-test-{os.getpid()}"
        try:
            files.ensure_private_dir(d)
            f = d / "k"
            f.write_text("x")
            files.make_private(f)
            why = files.private_file_problem(f)
            assert why and "profile" in why
        finally:
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(d)], capture_output=True)

    def test_a_symlink_or_junction_is_refused(self, tmp_path):
        target = tmp_path / "t"
        target.write_text("x")
        link = tmp_path / "l"
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("creating symlinks needs a privilege this account lacks")
        assert "link" in files.private_file_problem(link)

    def test_a_missing_file_raises_like_os_stat(self, tmp_path):
        with pytest.raises(OSError):
            files.private_file_problem(tmp_path / "absent")


# ── process liveness ──

def test_the_current_process_is_alive():
    assert process.pid_alive(os.getpid()) is True


def test_a_finished_process_is_not_alive():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert process.pid_alive(p.pid) is False


def test_the_probe_never_kills_the_process_it_asks_about():
    """``os.kill(pid, 0)`` terminates the process on Windows. The probe must leave a live process running, however often asked."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for _ in range(5):
            assert process.pid_alive(p.pid) is True
            time.sleep(0.05)
        assert p.poll() is None                                              # still running
    finally:
        p.kill()
        p.wait()
    assert process.pid_alive(p.pid) is False


def test_fork_join_uses_the_safe_probe():
    from agentic_os.mission import fork_join
    assert fork_join._pid_alive(os.getpid()) is True
