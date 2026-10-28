"""When a key is configured in the environment, NO key file is created on the data volume, and a stray one is never used or trusted.

Defect fixed in this release: ``agentic-os permissions init`` created ``<dir>/.permissions.key`` even though ``PERMISSIONS_KEY`` was set. The grants were sealed with the environment key (the store gives it
precedence), so the file sealed nothing, but it was a second 256-bit key sitting beside the ciphertext, contradicting "the key comes from the environment / a secret, never from a file on the data volume", and a later
unset of ``PERMISSIONS_KEY`` would have picked it up.

The rules, for ``PERMISSIONS_KEY`` (env), ``PERMISSIONS_KEY_FILE`` and ``<dir>/.permissions.key``:

* env set (valid)            -> it seals the store; no key file is created by ANY command; a stray file is never read, never a fallback, and is warned about (its content never printed)
* env set but invalid        -> an error, never a fallback to a file
* env unset, a key file      -> per the documented rules: an existing valid, 0600, owned-by-us file is used (with the existing warning); a loose one is refused
* env unset, no key file     -> `permissions init` (the explicit setup step) creates one at PERMISSIONS_KEY_FILE if that is set, else in the directory; nothing else ever creates one except the dev opt-in
* PERMISSIONS_REQUIRE_KEY=1  -> only the environment key; `init` refuses to create a key file
"""
from __future__ import annotations

import base64
import logging
import os
import pathlib

import pytest

from agentic_os import cli
from agentic_os.permissions import GrantStorageError, GrantStore

ENV_KEY = base64.b64encode(b"E" * 32).decode()
OTHER_ENV_KEY = base64.b64encode(b"O" * 32).decode()
FILE_KEY = base64.b64encode(b"F" * 32).decode()


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_REQUIRE_KEY", "PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "PERMISSIONS_ALLOW_PLAINTEXT_DEV", "PERMISSIONS_DIR", "AGENTIC_OS_PERMISSIONS"):
        monkeypatch.delenv(k, raising=False)
    import agentic_os.permissions as p
    p._WARNED_STRAY_KEYFILES.clear()
    yield


def perm(tmp_path) -> pathlib.Path:
    d = tmp_path / "perm"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def run(capsys, *args):
    rc = cli.main(["permissions", *args])
    out = capsys.readouterr()
    return rc, out.out + out.err


def names(d) -> set:
    return {p.name for p in pathlib.Path(d).iterdir()}


def stray(d, mode=0o600):
    f = pathlib.Path(d) / ".permissions.key"
    f.write_text(FILE_KEY)
    f.chmod(mode)
    return f


# ── init ──

def test_init_with_an_environment_key_creates_no_key_file_and_seals_with_the_environment_key(capsys, monkeypatch, tmp_path):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    rc, out = run(capsys, "init", "--dir", str(d))
    assert rc == 0 and ".permissions.key" not in names(d) and {"grants.enc", "profile.json"} <= names(d)
    assert "PERMISSIONS_KEY" in out and "no key file" in out.lower() and ".permissions.key (" not in out
    assert ENV_KEY not in out
    monkeypatch.setenv("PERMISSIONS_KEY_FILE", str(tmp_path / "nowhere"))
    assert len(GrantStore(str(d)).list()) > 0                                              # the environment key opens it


def test_init_key_only_with_an_environment_key_has_nothing_to_provision(capsys, monkeypatch, tmp_path):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    rc, out = run(capsys, "init", "--dir", str(d), "--key-only")
    assert rc == 0 and names(d) == set() and "PERMISSIONS_KEY" in out and "nothing" in out.lower()


def test_init_with_an_invalid_environment_key_is_an_error_and_creates_no_file(capsys, monkeypatch, tmp_path):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_KEY", "not-base64!!")
    rc, out = run(capsys, "init", "--dir", str(d))
    assert rc == 2 and "PERMISSIONS_KEY" in out and ".permissions.key" not in names(d) and "grants.enc" not in names(d)


def test_init_without_any_environment_key_still_provisions_the_documented_quickstart_key_file(capsys, tmp_path):
    d = perm(tmp_path)
    rc, out = run(capsys, "init", "--dir", str(d))
    assert rc == 0 and ".permissions.key" in names(d) and oct((d / ".permissions.key").stat().st_mode & 0o777) == "0o600"
    assert "created" in out and FILE_KEY not in out
    rc, out = run(capsys, "init", "--dir", str(d))                                         # idempotent: the key is kept
    assert rc == 0 and "kept" in out


def test_init_honours_an_explicit_key_file_location_instead_of_writing_into_the_data_directory(capsys, monkeypatch, tmp_path):
    d, secrets = perm(tmp_path), tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    monkeypatch.setenv("PERMISSIONS_KEY_FILE", str(secrets / "grants.key"))
    rc, out = run(capsys, "init", "--dir", str(d))
    assert rc == 0 and (secrets / "grants.key").is_file() and ".permissions.key" not in names(d)
    assert len(GrantStore(str(d)).list()) > 0


def test_init_refuses_to_create_a_key_file_when_only_the_environment_key_is_allowed(capsys, monkeypatch, tmp_path):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_REQUIRE_KEY", "1")
    rc, out = run(capsys, "init", "--dir", str(d))
    assert rc == 2 and "PERMISSIONS_REQUIRE_KEY" in out and "permissions key --env-file" in out and names(d) == set()
    rc, out = run(capsys, "init", "--dir", str(d), "--key-only")
    assert rc == 2 and names(d) == set()


def test_init_with_require_key_and_the_environment_key_works_and_creates_no_file(capsys, monkeypatch, tmp_path):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_REQUIRE_KEY", "1")
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    rc, out = run(capsys, "init", "--dir", str(d))
    assert rc == 0 and ".permissions.key" not in names(d)


# ── no command creates a key file when the environment key is set ──

def test_no_other_command_creates_a_key_file_when_the_environment_key_is_set(capsys, monkeypatch, tmp_path):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    assert run(capsys, "init", "--dir", str(d))[0] == 0
    for args in (["list"], ["revoke", "qs-local-guide.guide.retrieve"], ["audit-identifiers"], ["migrate", "--purge-backup"], ["recover-counter", "--confirm"], ["accept-rollback"]):
        run(capsys, *args, "--dir", str(d))
        assert ".permissions.key" not in names(d), args
    from agentic_os import app_gate
    monkeypatch.setenv("PERMISSIONS_DIR", str(d))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.reset_state()
    app_gate.install_gate("guide")
    app_gate.reset_state()
    assert ".permissions.key" not in names(d)


def test_the_control_plane_with_an_environment_key_creates_no_key_file(monkeypatch, tmp_path):
    import subprocess
    import sys
    d = perm(tmp_path)
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "AGENTIC_OS_"))}, "PERMISSIONS_KEY": ENV_KEY, "PERMISSIONS_DIR": str(d),
           "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1])}
    p = subprocess.run([sys.executable, "-c", "import agentic_os.control_plane as c; print(len(c._GRANTS.list()))"], capture_output=True, text=True, env=env, cwd=str(d), timeout=120)
    assert p.returncode == 0 and p.stdout.strip().endswith("2"), p.stderr[-300:]
    assert ".permissions.key" not in names(d) and "grants.enc" in names(d)


# ── a stray file next to an environment key ──

def test_a_stray_key_file_is_warned_about_by_path_only_and_never_used(monkeypatch, tmp_path, caplog):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    f = stray(d)
    with caplog.at_level(logging.WARNING):
        s = GrantStore(str(d))
        s.add("user", "u", "tool", "x.y", actions=["read"])
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert str(f) in msg and "PERMISSIONS_KEY is set" in msg and "not used" in msg and "remove" in msg.lower()
    assert FILE_KEY not in msg and ENV_KEY not in msg and "RkZGRk" not in msg                # never the contents (or any prefix of them)
    assert s._box.key_source == "env" and f.read_text() == FILE_KEY                                  # left alone: the operator decides


def test_the_stray_warning_is_given_once_per_file(monkeypatch, tmp_path, caplog):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    stray(d)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            GrantStore(str(d))
    assert sum("PERMISSIONS_KEY is set" in r.getMessage() for r in caplog.records) == 1


def test_no_warning_without_a_stray_file(monkeypatch, tmp_path, caplog):
    d = perm(tmp_path)
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    with caplog.at_level(logging.WARNING):
        GrantStore(str(d))
    assert not [r for r in caplog.records if "key file" in r.getMessage()]


def test_a_stray_key_file_never_takes_precedence_over_or_stands_in_for_the_environment_key(monkeypatch, tmp_path):
    d = perm(tmp_path)
    stray(d)
    monkeypatch.setenv("PERMISSIONS_KEY", ENV_KEY)
    GrantStore(str(d)).add("user", "u", "tool", "x.y", actions=["read"])
    monkeypatch.setenv("PERMISSIONS_KEY", OTHER_ENV_KEY)                                    # the environment key changed: the file must NOT quietly take over
    s = GrantStore(str(d))
    assert s.list() == []                                                                   # sealed under another key: fails closed, honours nothing (the file's key is not tried)
    monkeypatch.setenv("PERMISSIONS_KEY", "not-base64!!")                                   # an invalid environment key: an error, not a fallback to the file
    with pytest.raises(GrantStorageError, match="PERMISSIONS_KEY"):
        GrantStore(str(d))


def test_a_blank_environment_key_is_unset_and_the_documented_file_rules_apply(monkeypatch, tmp_path, caplog):
    d = perm(tmp_path)
    stray(d)
    monkeypatch.setenv("PERMISSIONS_KEY", "   ")
    with caplog.at_level(logging.WARNING):
        s = GrantStore(str(d))
    assert s._box.key_source == "keyfile" and any("using the existing on-box key file" in r.getMessage() for r in caplog.records)


def test_with_no_environment_key_a_loose_key_file_is_refused_and_a_tight_one_is_used(monkeypatch, tmp_path):
    d = perm(tmp_path)
    f = stray(d, 0o644)
    with pytest.raises(GrantStorageError, match="wider than 0600"):
        GrantStore(str(d))
    f.chmod(0o600)
    assert GrantStore(str(d))._box.key_source == "keyfile"


def test_with_require_key_a_key_file_is_never_used(monkeypatch, tmp_path):
    d = perm(tmp_path)
    stray(d)
    monkeypatch.setenv("PERMISSIONS_REQUIRE_KEY", "1")
    with pytest.raises(GrantStorageError, match="PERMISSIONS_REQUIRE_KEY"):
        GrantStore(str(d))
