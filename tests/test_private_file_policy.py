"""The private-file policy where a development switch is set (runs on every OS by faking the platform's mode-bit/DACL answer).

On a platform without POSIX modes (Windows) an exposed key file is refused and NO environment switch overrides that: the dev opt-in
`PERMISSIONS_ALLOW_GENERATED_KEY_DEV` still means "generate a key file if there is none" there, but it never means "use a key file that others
can read". (On POSIX the existing, loudly logged opt-in behaviour is unchanged: it uses the loose file with a warning.)"""
from __future__ import annotations

import base64

import pytest

from agentic_os import permissions as perm
from agentic_os._platform import files
from agentic_os.permissions import GrantStorageError, GrantStore


@pytest.fixture
def exposed_key_dir(tmp_path, monkeypatch):
    for v in ("PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_ALLOW_PLAINTEXT_DEV"):
        monkeypatch.delenv(v, raising=False)
    d = tmp_path / "vol"
    d.mkdir()
    (d / ".permissions.key").write_text(base64.b64encode(b"k" * 32).decode())
    monkeypatch.setattr(perm, "_key_file_exposure", lambda path: "grants access to S-1-1-0, which is not the running user")
    return d


def test_without_posix_modes_the_dev_opt_in_does_not_use_an_exposed_key_file(exposed_key_dir, monkeypatch):
    monkeypatch.setattr(files, "HAS_POSIX_MODES", False)
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
    with pytest.raises(GrantStorageError, match="S-1-1-0"):
        GrantStore(str(exposed_key_dir))


def test_with_posix_modes_the_documented_dev_opt_in_is_unchanged(exposed_key_dir, monkeypatch, caplog):
    monkeypatch.setattr(files, "HAS_POSIX_MODES", True)
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
    s = GrantStore(str(exposed_key_dir))
    assert s.encrypted and "DEVELOPMENT ONLY" in caplog.text
