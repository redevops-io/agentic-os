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
