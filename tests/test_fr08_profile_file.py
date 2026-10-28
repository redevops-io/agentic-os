"""FR-08: the local profile file is read like a secret: no symlink, a regular file owned by the running user, not readable or writable by anyone else (0600). A profile that fails any
check is an ERROR (the app refuses to start under basic permissions), never a silent fallback to another user; a missing file is the pre-profile default user, with a warning."""
from __future__ import annotations

import json
import os
import pathlib
import stat

import pytest

from agentic_os import app_gate
from agentic_os.app_gate import GateStartupError, install_gate
from agentic_os.permissions import GrantStorageError
from agentic_os.permissions_setup import DEFAULT_USER, init_local_profile, local_user

APP = "growth-assistant"


@pytest.fixture()
def d(tmp_path, monkeypatch):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    init_local_profile(str(tmp_path / "perm"), user="alice")
    return tmp_path / "perm"


def test_the_written_profile_is_a_private_regular_file(d):
    st = os.lstat(d / "profile.json")
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == os.geteuid()
    assert local_user(str(d)) == "alice"


def test_a_symlinked_profile_is_refused_even_when_it_points_at_a_valid_one(d, tmp_path):
    other = tmp_path / "mallory.json"
    other.write_text(json.dumps({"version": 1, "user": "mallory"}))
    os.chmod(other, 0o600)
    (d / "profile.json").unlink()
    os.symlink(other, d / "profile.json")
    with pytest.raises(GrantStorageError) as e:
        local_user(str(d))
    assert "symlink" in str(e.value).lower() or "not a regular" in str(e.value).lower()
    with pytest.raises(GateStartupError):
        install_gate(APP)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604, 0o666, 0o700])
def test_a_profile_that_others_can_read_or_write_or_that_is_executable_is_refused(d, mode):
    os.chmod(d / "profile.json", mode)
    with pytest.raises(GrantStorageError) as e:
        local_user(str(d))
    assert "0600" in str(e.value) and "alice" not in str(e.value)
    with pytest.raises(GateStartupError):
        install_gate(APP)


def test_a_profile_owned_by_someone_else_is_refused(d, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: os.lstat(d / "profile.json").st_uid + 1)
    with pytest.raises(GrantStorageError) as e:
        local_user(str(d))
    assert "owned" in str(e.value)


def test_a_directory_or_fifo_in_place_of_the_file_is_refused(d):
    (d / "profile.json").unlink()
    (d / "profile.json").mkdir()
    with pytest.raises(GrantStorageError):
        local_user(str(d))


def test_a_missing_profile_is_the_pre_profile_default_user_with_a_warning(d, caplog):
    (d / "profile.json").unlink()
    with caplog.at_level("WARNING"):
        assert local_user(str(d)) == DEFAULT_USER
    assert "profile.json" in caplog.text and "init" in caplog.text


def test_tampered_content_is_an_error_that_does_not_echo_the_value(d):
    for bad in ("", "[]", "{}", '{"user": 5}', '{"user": " pad"}', '{"user": "SECRET\\u200bname"}', "not json"):
        (d / "profile.json").write_text(bad)
        os.chmod(d / "profile.json", 0o600)
        with pytest.raises(GrantStorageError) as e:
            local_user(str(d))
        assert "SECRET" not in str(e.value) and "pad" not in str(e.value).replace("a valid identifier", "")
