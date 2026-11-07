"""FR-08: the local profile file is read like a secret: no symlink, a regular file owned by the running user, not readable or writable by anyone else (0600). A profile that fails any
check is an ERROR (the app refuses to start under basic permissions), never a silent fallback to another user; a missing file is the pre-profile default user, with a warning."""
from __future__ import annotations

import json
import os
import pathlib
import stat

import pytest
from tests._posix_marks import posix_modes

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


@posix_modes
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
@posix_modes
def test_a_profile_that_others_can_read_or_write_or_that_is_executable_is_refused(d, mode):
    os.chmod(d / "profile.json", mode)
    with pytest.raises(GrantStorageError) as e:
        local_user(str(d))
    assert "0600" in str(e.value) and "alice" not in str(e.value)
    with pytest.raises(GateStartupError):
        install_gate(APP)


@posix_modes
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


#: A high-entropy marker that cannot occur in a filesystem path (a temp directory may legitimately be called "scratchpad", "pad", "SECRET"...). Every tampered file
#: below carries it, so "the message does not echo the file" is checked against content that is distinguishable from the path the message is allowed to contain.
SENTINEL = "zq9x7-tampered-sentinel"

#: tampered content -> the only message (with the profile path replaced by <PATH>) the loader may raise for it: a fixed template plus an exception TYPE NAME, never a value.
TAMPERED = {
    "": "the local profile <PATH> cannot be read (JSONDecodeError)",
    "[]": "the local profile <PATH> cannot be read (TypeError)",
    "{}": "the local profile <PATH> cannot be read (KeyError)",
    '{"user": 5}': "the local profile <PATH> names a user that is not a valid identifier",
    '{"user": " ' + SENTINEL + '"}': "the local profile <PATH> names a user that is not a valid identifier",
    '{"user": "SECRET\\u200b' + SENTINEL + '"}': "the local profile <PATH> names a user that is not a valid identifier",
    '{"user": "x", "extra": "SECRET ' + SENTINEL + '"} trailing': "the local profile <PATH> cannot be read (JSONDecodeError)",
    "not json " + SENTINEL: "the local profile <PATH> cannot be read (JSONDecodeError)",
}


def assert_message_does_not_echo_the_file(message: str, path, content: str) -> None:
    """The message may name the profile PATH (the operator's own configured location) and a fixed template, nothing of the FILE'S CONTENT."""
    generic = message.replace(str(path), "<PATH>")        # the path is the operator's own configured location, not file content
    assert SENTINEL not in message, "the message echoes (part of) the tampered file"
    assert "SECRET" not in generic, "the message echoes (part of) the tampered file"
    assert generic == TAMPERED[content], f"the message is not the fixed template for this failure: {generic!r}"


def _tamper_and_load(directory):
    for bad in TAMPERED:
        (directory / "profile.json").write_text(bad)
        os.chmod(directory / "profile.json", 0o600)
        with pytest.raises(GrantStorageError) as e:
            local_user(str(directory))
        assert_message_does_not_echo_the_file(str(e.value), directory / "profile.json", bad)


def test_tampered_content_is_an_error_that_does_not_echo_the_value(d):
    _tamper_and_load(d)


def test_the_no_echo_check_does_not_depend_on_where_the_temp_directory_is(tmp_path, monkeypatch):
    """The assertion used to look for the text "pad", which a temp path such as .../scratchpad/... contains: a false failure. It now looks for content
    that cannot be part of a path, so it holds under a directory literally named like the old probe."""
    perm = tmp_path / "scratchpad_pad_SECRET"          # a path that contains "scratchpad", "pad" and "SECRET"
    monkeypatch.setenv("PERMISSIONS_DIR", str(perm))
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
    init_local_profile(str(perm), user="alice")
    assert "pad" in str(perm / "profile.json") and "scratchpad" in str(perm / "profile.json")
    _tamper_and_load(perm)


@pytest.mark.parametrize("echoed", [
    "the local profile <PATH> names a user that is not a valid identifier: " + SENTINEL,           # appends the value
    "the local profile <PATH> names a user that is not a valid identifier (' " + SENTINEL + "')",   # repr of the value
    "the local profile <PATH> cannot be read (JSONDecodeError: not json " + SENTINEL + ")",         # an exception text carrying the content
    "SECRET names a user that is not a valid identifier",
    "the local profile <PATH> names a user that is not a valid identifier ZQ" ,                    # any other deviation from the template
])
def test_the_no_echo_check_still_fails_when_the_loader_would_echo_the_file(tmp_path, echoed):
    """Mutation proof for the assertion itself: a message that echoes any part of the file, or deviates from the fixed template, is rejected."""
    path = tmp_path / "profile.json"
    content = '{"user": " ' + SENTINEL + '"}'
    with pytest.raises(AssertionError):
        assert_message_does_not_echo_the_file(echoed.replace("<PATH>", str(path)), path, content)


def test_a_loader_that_echoes_the_value_fails_the_check(d, monkeypatch):
    """The same, end to end: replace the loader's identifier check by one that raises with the user value, and the real test body must fail."""
    from agentic_os import identifiers

    def echoing(user):
        raise GrantStorageError(f"the local profile {d / 'profile.json'} names a user that is not a valid identifier: {user!r}")
    monkeypatch.setattr(identifiers, "valid_identifier", echoing)
    with pytest.raises(AssertionError):
        _tamper_and_load(d)
