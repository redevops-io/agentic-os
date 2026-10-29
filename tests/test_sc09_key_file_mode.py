"""SC-09: an existing key file wider than 0600, or owned by someone else, is refused (fail closed) with an actionable error unless the
explicit development opt-in is set — then it is used with a warning. It is never silently used."""
from __future__ import annotations

import base64
import logging
import os
import pathlib
import stat
import subprocess
import sys

import pytest

from agentic_os import permissions as P
from agentic_os.permissions import GrantStorageError, GrantStore

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _key_dir(tmp_path, mode):
    d = tmp_path / "vol"
    P.provision_key_file(str(d))
    os.chmod(d / ".permissions.key", mode)
    return d


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "PERMISSIONS_ALLOW_PLAINTEXT_DEV",
              "PERMISSIONS_REQUIRE_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o700 | 0o010])
def test_a_key_file_wider_than_0600_is_refused_with_the_fix(tmp_path, mode):
    d = _key_dir(tmp_path, mode)
    with pytest.raises(GrantStorageError) as e:
        GrantStore(str(d))
    msg = str(e.value)
    assert f"{mode:04o}" in msg and "chmod 600" in msg and "PERMISSIONS_KEY" in msg and str(d / ".permissions.key") in msg
    assert not (d / "grants.enc").exists()                                 # nothing was encrypted with it


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_0600_and_narrower_are_accepted(tmp_path, mode):
    d = _key_dir(tmp_path, mode)
    assert GrantStore(str(d)).status()["key_source"] == "keyfile"


def test_a_key_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    d = _key_dir(tmp_path, 0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(d / ".permissions.key").st_uid + 1)
    with pytest.raises(GrantStorageError, match="not owned"):
        GrantStore(str(d))


def test_the_dev_opt_in_uses_it_with_a_warning(tmp_path, caplog):
    d = _key_dir(tmp_path, 0o644)
    with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
        s = GrantStore(str(d), allow_generated_key_dev=True)
    assert s.status()["key_source"] == "keyfile"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "0644" in text and "wider than 0600" in text and "DEVELOPMENT" in text


def test_the_plaintext_dev_opt_in_does_not_make_a_loose_key_file_usable(tmp_path):
    d = _key_dir(tmp_path, 0o644)
    s = GrantStore(str(d), allow_plaintext_dev=True)
    assert s.encrypted is False                                            # the unusable key file is not used: plaintext dev store


def test_permissions_key_file_is_checked_too(tmp_path, monkeypatch):
    d = _key_dir(tmp_path, 0o644)
    monkeypatch.setenv("PERMISSIONS_KEY_FILE", str(d / ".permissions.key"))
    with pytest.raises(GrantStorageError, match="chmod 600"):
        GrantStore(str(tmp_path / "other"))


def test_a_key_in_the_environment_is_unaffected_by_a_loose_key_file(tmp_path, monkeypatch):
    d = _key_dir(tmp_path, 0o644)
    monkeypatch.setenv("PERMISSIONS_KEY", base64.b64encode(b"k" * 32).decode())
    assert GrantStore(str(d)).status()["key_source"] == "env"


def test_the_message_never_contains_the_key(tmp_path):
    d = _key_dir(tmp_path, 0o644)
    key = (d / ".permissions.key").read_text().strip()
    with pytest.raises(GrantStorageError) as e:
        GrantStore(str(d))
    assert key not in str(e.value)


def test_init_on_a_loose_key_file_is_refused(tmp_path):
    d = _key_dir(tmp_path, 0o644)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    env["PYTHONPATH"] = str(ROOT)
    r = subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", "init", "--dir", str(d)], env=env, cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and "chmod 600" in r.stderr
