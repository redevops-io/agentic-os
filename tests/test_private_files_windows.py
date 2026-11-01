"""Windows counterparts of the POSIX mode-bit tests: the same refusals, decided from the DACL and the file's location.

POSIX proves "a key file wider than 0600 is refused" by chmod; Windows has no mode bits, so these tests widen the DACL (``icacls``)
and check that the grant store, the local profile and the encrypted secret store refuse the same way, name the cause and the fix, and
that what the code itself creates is accepted. They run on the Windows lane and are skipped elsewhere (the POSIX tests cover those)."""
from __future__ import annotations

import base64
import os
import subprocess

import pytest

from agentic_os import permissions as perm
from agentic_os._platform import files
from agentic_os.permissions import GrantStorageError, GrantStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="DACL checks are Windows; the POSIX mode-bit tests cover Linux and macOS")

KEY = base64.b64encode(b"k" * 32).decode()


def _icacls(*args: str) -> None:
    subprocess.run(["icacls", *args], check=True, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    monkeypatch.delenv("PERMISSIONS_KEY_FILE", raising=False)
    monkeypatch.delenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", raising=False)
    monkeypatch.delenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", raising=False)


def test_a_key_file_provisioned_by_init_is_private_and_is_used(tmp_path):
    kf, created = perm.provision_key_file(str(tmp_path / "d"))
    assert created and files.private_file_problem(kf) is None
    s = GrantStore(str(tmp_path / "d"))
    s.add("app", "support", "table", "crm.customers")
    assert [g.subject for g in GrantStore(str(tmp_path / "d")).list()] == ["support"]


def test_a_key_file_that_everyone_can_read_is_refused_and_the_message_names_the_fix(tmp_path):
    kf, _ = perm.provision_key_file(str(tmp_path / "d"))
    _icacls(str(kf), "/grant", "*S-1-1-0:R")                              # Everyone: read
    with pytest.raises(GrantStorageError) as e:
        GrantStore(str(tmp_path / "d"))
    msg = str(e.value)
    assert "S-1-1-0" in msg and "icacls" in msg and "PERMISSIONS_KEY" in msg
    files.make_private(kf)                                               # repaired: accepted again
    GrantStore(str(tmp_path / "d"))


def test_a_key_file_outside_the_profile_is_refused_even_with_a_private_dacl(tmp_path):
    public = os.environ.get("PUBLIC")
    if not public or not os.access(public, os.W_OK):
        pytest.skip("no writable location outside the user profile on this machine")
    d = os.path.join(public, f"agentic-os-keytest-{os.getpid()}")
    try:
        kf, _ = perm.provision_key_file(d)
        assert "profile" in files.private_file_problem(kf)
        with pytest.raises(GrantStorageError, match="profile"):
            GrantStore(d)
    finally:
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", d], capture_output=True)


def test_a_store_directory_created_by_the_store_is_private(tmp_path):
    d = tmp_path / "fresh" / "grants"
    s = GrantStore(str(d), allow_generated_key_dev=True)
    s.add("app", "support", "table", "crm.customers")
    for p in (d, d / "grants.enc", d / ".permissions.key", d / ".grants.lock"):
        assert files.private_file_problem(p) is None, p


def test_a_profile_that_others_can_read_is_refused(tmp_path):
    from agentic_os.permissions_setup import local_user, write_profile
    d = str(tmp_path / "prof")
    write_profile(d, "alice")
    assert local_user(d) == "alice"
    _icacls(os.path.join(d, "profile.json"), "/grant", "*S-1-1-0:R")
    with pytest.raises(GrantStorageError, match="S-1-1-0"):
        local_user(d)


def test_the_encrypted_secret_store_refuses_an_exposed_file_and_writes_private_ones(tmp_path):
    from agentic_os.secrets.stores import EncryptedFileSecretStore
    from runtime_contracts.protocol.secrets import SecretRef
    from runtime_contracts.secrets_local.store import SecretAccessError
    store = EncryptedFileSecretStore(str(tmp_path / "s"), key=b"m" * 32)
    ref = store.put(namespace="ns", path="api", value=b"value")
    target = store._resolve("ns", "api")
    assert files.private_file_problem(target) is None and store._read(ref) == b"value"
    _icacls(target, "/grant", "*S-1-1-0:R")
    with pytest.raises(SecretAccessError, match="not private"):
        store._read(ref)


def test_the_plaintext_dev_opt_in_does_not_make_an_exposed_key_file_usable(tmp_path):
    kf, _ = perm.provision_key_file(str(tmp_path / "d"))
    _icacls(str(kf), "/grant", "*S-1-1-0:R")
    s = GrantStore(str(tmp_path / "d"), allow_plaintext_dev=True)
    assert s.encrypted is False                                          # the unusable key file is not used: a plaintext dev store


def test_the_refusal_never_contains_the_key(tmp_path):
    kf, _ = perm.provision_key_file(str(tmp_path / "d"))
    key = open(kf, encoding="ascii").read().strip()
    _icacls(str(kf), "/grant", "*S-1-1-0:R")
    with pytest.raises(GrantStorageError) as e:
        GrantStore(str(tmp_path / "d"))
    assert key not in str(e.value)


# ── the files whose POSIX tests assert a 0600 mode: the same properties from the DACL ──

def test_the_env_file_written_for_a_key_is_private_and_is_lf_only(tmp_path):
    from agentic_os.permissions_setup import write_key_env_file
    p = write_key_env_file(str(tmp_path / "e" / "permissions.env"))
    assert files.private_file_problem(p) is None
    raw = open(p, "rb").read()
    assert raw.startswith(b"PERMISSIONS_KEY=") and b"\r" not in raw and raw.endswith(b"\n")


def test_the_plaintext_migration_backup_and_the_rollback_counter_are_private(tmp_path, monkeypatch):
    d = tmp_path / "g"
    GrantStore(str(d), allow_plaintext_dev=True).add("app", "support", "table", "crm.customers")    # an old plaintext store
    assert (d / "grants.jsonl").exists()
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    GrantStore(str(d))                                                                              # migrates it, keeping an UNENCRYPTED backup
    backups = list(d.glob("grants.jsonl.migrated-*"))
    assert backups and all(files.private_file_problem(b) is None for b in backups)
    GrantStore(str(d)).add("app", "other", "table", "crm.customers")
    assert files.private_file_problem(d / "grants.counter") is None


def test_init_key_only_creates_a_private_key_file_and_no_grants(tmp_path):
    from agentic_os.permissions_setup import init_local_profile
    d = tmp_path / "perm"
    r = init_local_profile(str(d), key_only=True)
    assert r["key_created"] and files.private_file_problem(d / ".permissions.key") is None and not (d / "grants.enc").exists()


def test_init_on_an_exposed_key_file_is_refused_with_the_icacls_fix(tmp_path):
    from agentic_os.permissions_setup import init_local_profile
    d = tmp_path / "perm"
    kf, _ = perm.provision_key_file(str(d))
    _icacls(str(kf), "/grant", "*S-1-1-0:R")
    with pytest.raises(GrantStorageError) as e:
        init_local_profile(str(d))
    assert "icacls" in str(e.value) and "S-1-1-0" in str(e.value)


def test_the_dev_opt_in_does_not_use_an_exposed_key_file_on_windows(tmp_path, monkeypatch):
    kf, _ = perm.provision_key_file(str(tmp_path / "d"))
    _icacls(str(kf), "/grant", "*S-1-1-0:R")
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
    with pytest.raises(GrantStorageError, match="S-1-1-0"):
        GrantStore(str(tmp_path / "d"))


# ── private from creation: a failure to harden a file must not leave it exposed ──

@pytest.fixture
def open_dir(tmp_path):
    """A directory whose DACL gives Everyone inheritable read: whatever is created inside inherits an exposed ACL unless it is created private."""
    d = tmp_path / "open"
    d.mkdir()
    _icacls(str(d), "/grant", "*S-1-1-0:(OI)(CI)R")
    return d


def _never_exposed(*paths):
    for p in paths:
        assert not os.path.exists(p) or files.private_file_problem(p) is None, f"{p} was left exposed"


def test_a_key_file_is_private_from_creation_even_if_the_hardening_step_fails(open_dir, monkeypatch):
    from agentic_os._platform import _win32

    def boom(*a, **k):
        raise OSError("injected")
    monkeypatch.setattr(_win32, "set_private_dacl", boom)
    keys = open_dir / "keys"
    keys.mkdir()
    try:
        perm.provision_key_file(str(keys))
    except OSError:
        pass
    _never_exposed(keys / ".permissions.key")


def test_an_env_key_file_is_private_from_creation_even_if_the_hardening_step_fails(open_dir, monkeypatch):
    from agentic_os._platform import _win32
    from agentic_os.permissions_setup import write_key_env_file

    def boom(*a, **k):
        raise OSError("injected")
    monkeypatch.setattr(_win32, "set_private_dacl", boom)
    target = open_dir / "permissions.env"
    try:
        write_key_env_file(str(target))
    except OSError:
        pass
    _never_exposed(target)


def test_an_atomic_write_never_leaves_an_exposed_file_even_if_the_hardening_step_fails(open_dir, monkeypatch):
    from agentic_os._platform import _win32

    def boom(*a, **k):
        raise OSError("injected")
    monkeypatch.setattr(_win32, "set_private_dacl", boom)
    try:
        files.atomic_write(open_dir / "secret.bin", b"sealed")
    except OSError:
        pass
    _never_exposed(*open_dir.iterdir())


def test_the_temp_file_of_an_atomic_write_is_private_before_any_data_is_written(open_dir):
    fd, tmp = files.create_private_temp(str(open_dir), "t.", ".tmp")
    try:
        assert files.private_file_problem(tmp) is None
    finally:
        os.close(fd)
        os.unlink(tmp)


def test_the_event_log_the_outcome_store_and_the_fork_lock_are_created_private(open_dir):
    from agentic_os.mission.fork_join import _FileLock
    from agentic_os.mission.store import EventStore
    from agentic_os.outcome_store import FileOutcomeStore
    from agentic_os.priority_engine import OutcomeEvent
    log = open_dir / "log.jsonl"
    store = EventStore(str(log))
    store.append("A", "m1", {"n": 1})
    with _FileLock(store):
        pass
    out = open_dir / "outcomes.jsonl"
    FileOutcomeStore(str(out)).append(OutcomeEvent(candidate_id="c1", source_app="app", accepted=True, event_id="e1"))
    for p in (log, open_dir / "log.jsonl.forklock", out, open_dir / "outcomes.jsonl.lock"):
        assert p.exists() and files.private_file_problem(p) is None, f"{p.name} was created exposed"
