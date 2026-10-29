"""Grant storage — no implicit plaintext, and a deliberate path for plaintext files that already exist.

    no cryptography / no usable key   -> the store refuses to start, unless PERMISSIONS_ALLOW_PLAINTEXT_DEV is on
    the dev-only setting is loud      -> a warning at start-up and on every write
    an existing plaintext grants file -> encrypted in place at start-up (backup first, verified, atomic), the backup kept for rollback
"""
from __future__ import annotations

import base64
import json
import logging
import os

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import GrantStore

KEY = base64.b64encode(b"k" * 32).decode()
LEGACY = {"version": 1, "grants": [
    {"id": "g-1", "subject_kind": "app", "subject": "support", "resource_kind": "table",
     "resource_name": "crm.customers", "actions": ["read"], "row_scope": "in", "row_column": "region",
     "row_values": ["us"], "masked_columns": ["email"], "created_by": "old", "at": 1.0}]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_ALLOW_PLAINTEXT_DEV",
              "PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "PERMISSIONS_MIGRATE_PLAINTEXT", "PERMISSIONS_REQUIRE_KEY",
              "PERMISSIONS_DIR"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def no_crypto(monkeypatch):
    monkeypatch.setattr(perm, "_HAVE_CRYPTO", False)


def _legacy(tmp_path):
    (tmp_path / "grants.jsonl").write_text(json.dumps(LEGACY))


def _add(store, subject="support"):
    return store.add("app", subject, "table", "crm.customers", row_scope="in", row_column="region",
                     row_values=["us"], masked_columns=["email"])


# ───────────────────────── no implicit plaintext ─────────────────────────


class TestNoImplicitPlaintext:
    def test_without_cryptography_the_store_refuses_to_start(self, tmp_path, no_crypto):
        with pytest.raises(RuntimeError, match="PERMISSIONS_ALLOW_PLAINTEXT_DEV"):
            GrantStore(str(tmp_path))

    def test_the_error_names_the_cause(self, tmp_path, no_crypto):
        with pytest.raises(RuntimeError, match="cryptography"):
            GrantStore(str(tmp_path))

    def test_nothing_is_written_when_it_refuses(self, tmp_path, no_crypto):
        with pytest.raises(RuntimeError):
            GrantStore(str(tmp_path))
        assert list(tmp_path.iterdir()) == []

    def test_a_key_that_cannot_be_established_refuses_to_start(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY_FILE", str(tmp_path / "afile" / "key"))
        (tmp_path / "afile").write_text("not a directory")      # the key file's parent is a regular file
        with pytest.raises(RuntimeError, match="key"):
            GrantStore(str(tmp_path / "d"))

    @pytest.mark.parametrize("bad", ["not base64 !!", base64.b64encode(b"short").decode()])
    def test_an_invalid_permissions_key_is_an_error_not_a_silent_fallback(self, tmp_path, monkeypatch, bad):
        monkeypatch.setenv("PERMISSIONS_KEY", bad)
        with pytest.raises(RuntimeError, match="PERMISSIONS_KEY"):
            GrantStore(str(tmp_path))

    def test_a_damaged_key_file_is_an_error(self, tmp_path, monkeypatch):
        kf = tmp_path / "bad.key"
        kf.write_text("%%%")
        monkeypatch.setenv("PERMISSIONS_KEY_FILE", str(kf))
        with pytest.raises(RuntimeError, match="key"):
            GrantStore(str(tmp_path / "d"))

    def test_a_valid_key_encrypts_at_rest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        s = GrantStore(str(tmp_path))
        _add(s, "visible-subject")
        assert s.encrypted and s.status()["key_source"] == "env"
        assert b"visible-subject" not in (tmp_path / "grants.enc").read_bytes()
        assert not (tmp_path / "grants.jsonl").exists()

    def test_the_generated_on_box_key_works_only_with_the_explicit_dev_opt_in(self, tmp_path):
        s = GrantStore(str(tmp_path), allow_generated_key_dev=True)
        _add(s)
        assert s.encrypted and s.status()["key_source"] == "keyfile"


class TestTheDevelopmentOnlySetting:
    def test_it_allows_plaintext_without_cryptography(self, tmp_path, no_crypto, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", "1")
        s = GrantStore(str(tmp_path))
        _add(s)
        assert s.encrypted is False and (tmp_path / "grants.jsonl").exists()
        assert s.status()["plaintext_dev"] is True and len(s.list()) == 1

    def test_it_warns_at_start_up_and_on_every_write(self, tmp_path, no_crypto, monkeypatch, caplog):
        monkeypatch.setenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", "1")
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            s = GrantStore(str(tmp_path))
            n_start = sum("PLAINTEXT" in r.getMessage() for r in caplog.records)
            _add(s, "a")
            _add(s, "b")
            s.remove("nope")
            n_after = sum("PLAINTEXT" in r.getMessage() for r in caplog.records)
        assert n_start == 1 and n_after == 3

    def test_the_constructor_argument_overrides_the_environment(self, tmp_path, no_crypto, monkeypatch):
        s = GrantStore(str(tmp_path), allow_plaintext_dev=True)
        assert s.encrypted is False
        monkeypatch.setenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", "1")
        with pytest.raises(RuntimeError):
            GrantStore(str(tmp_path / "other"), allow_plaintext_dev=False)

    @pytest.mark.parametrize("val,on", [("1", True), ("true", True), ("YES", True), ("0", False),
                                        ("", False), ("false", False), ("off", False), ("maybe", False)])
    def test_only_clear_yes_values_turn_it_on(self, tmp_path, no_crypto, monkeypatch, val, on):
        monkeypatch.setenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", val)
        if on:
            assert GrantStore(str(tmp_path)).encrypted is False
        else:
            with pytest.raises(RuntimeError):
                GrantStore(str(tmp_path))

    def test_it_does_not_downgrade_an_encrypted_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _add(GrantStore(str(tmp_path)))
        monkeypatch.delenv("PERMISSIONS_KEY")
        monkeypatch.setattr(perm, "_HAVE_CRYPTO", False)           # e.g. a rebuilt image without the package
        with pytest.raises(RuntimeError, match="grants.enc"):
            GrantStore(str(tmp_path), allow_plaintext_dev=True)

    def test_a_tampered_encrypted_file_still_seals_the_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        s = GrantStore(str(tmp_path))
        _add(s)
        blob = bytearray((tmp_path / "grants.enc").read_bytes())
        blob[-1] ^= 1
        (tmp_path / "grants.enc").write_bytes(bytes(blob))
        assert s.list() == [] and s.sealed is True


# ───────────────────────── existing plaintext files ─────────────────────────


class TestMigratingAnExistingPlaintextFile:
    def test_start_up_encrypts_it_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _legacy(tmp_path)
        s = GrantStore(str(tmp_path))
        assert s.encrypted and [g.id for g in s.list()] == ["g-1"]
        assert (tmp_path / "grants.enc").exists() and not (tmp_path / "grants.jsonl").exists()
        assert b"support" not in (tmp_path / "grants.enc").read_bytes()

    def test_the_plaintext_copy_is_kept_for_rollback_and_reported(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _legacy(tmp_path)
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            s = GrantStore(str(tmp_path))
        backups = list(tmp_path.glob("grants.jsonl.migrated-*"))
        assert len(backups) == 1 and json.loads(backups[0].read_text()) == LEGACY
        assert s.status()["migrated_from_plaintext"] == 1
        assert any("plaintext backup" in r.getMessage() for r in caplog.records)

    def test_it_happens_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _legacy(tmp_path)
        GrantStore(str(tmp_path))
        s = GrantStore(str(tmp_path))
        assert [g.id for g in s.list()] == ["g-1"] and len(list(tmp_path.glob("grants.jsonl.migrated-*"))) == 1
        assert s.status()["migrated_from_plaintext"] == 0

    def test_it_can_be_switched_off_and_then_start_up_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        monkeypatch.setenv("PERMISSIONS_MIGRATE_PLAINTEXT", "0")
        _legacy(tmp_path)
        with pytest.raises(RuntimeError, match="PERMISSIONS_MIGRATE_PLAINTEXT"):
            GrantStore(str(tmp_path))
        assert (tmp_path / "grants.jsonl").exists() and not (tmp_path / "grants.enc").exists()

    def test_a_plaintext_file_that_does_not_parse_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        (tmp_path / "grants.jsonl").write_text("{ not json")
        with pytest.raises(RuntimeError, match="grants.jsonl"):
            GrantStore(str(tmp_path))
        assert (tmp_path / "grants.jsonl").read_text() == "{ not json" and not (tmp_path / "grants.enc").exists()

    def test_a_failure_while_encrypting_leaves_the_plaintext_file_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _legacy(tmp_path)

        def boom(self, plaintext):
            raise OSError("disk on fire")
        monkeypatch.setattr(perm._SecretBox, "seal", boom)
        with pytest.raises(RuntimeError):
            GrantStore(str(tmp_path))
        assert (tmp_path / "grants.jsonl").exists() and not (tmp_path / "grants.enc").exists()

    def test_when_both_files_exist_the_encrypted_one_wins_and_the_stale_one_is_flagged(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        s = GrantStore(str(tmp_path))
        _add(s, "fresh")
        _legacy(tmp_path)
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            s2 = GrantStore(str(tmp_path))
        assert [g.subject for g in s2.list()] == ["fresh"] and (tmp_path / "grants.jsonl").exists()
        assert any("stale" in r.getMessage() for r in caplog.records)

    def test_the_documented_rollback_restores_the_plaintext_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _legacy(tmp_path)
        GrantStore(str(tmp_path))
        (tmp_path / "grants.enc").unlink()
        (tmp_path / "grants.counter").unlink()           # the rollback-protection sidecar goes with the encrypted store
        next(tmp_path.glob("grants.jsonl.migrated-*")).rename(tmp_path / "grants.jsonl")
        monkeypatch.delenv("PERMISSIONS_KEY")
        monkeypatch.setattr(perm, "_HAVE_CRYPTO", False)           # the setup the plaintext file came from
        s = GrantStore(str(tmp_path), allow_plaintext_dev=True)
        assert [g.id for g in s.list()] == ["g-1"]


# ───────────────────────── migration safeguards ─────────────────────────

import stat

SECRET_SUBJECT = "zz-very-secret-subject"
SECRET_RESOURCE = "zz.secret.resource"
LEGACY_SECRET = {"version": 1, "grants": [
    {"id": "g-s", "subject_kind": "app", "subject": SECRET_SUBJECT, "resource_kind": "table",
     "resource_name": SECRET_RESOURCE, "actions": ["read"], "row_scope": "in", "row_column": "region",
     "row_values": ["zz-secret-value"], "masked_columns": ["email"], "created_by": "old", "at": 1.0}]}


class _Crash(BaseException):
    """Stands in for a process dying mid-step (not an Exception, so nothing may swallow it)."""


def _payload():
    return json.dumps({"version": 1, "grants": [perm.Grant.from_dict(d).to_dict() for d in LEGACY_SECRET["grants"]],
                       "meta": {"counter": 1}}).encode()


def _write_legacy(tmp_path, data=None):
    p = tmp_path / "grants.jsonl"
    p.write_text(json.dumps(data or LEGACY_SECRET))
    return p, p.read_bytes()


def _decrypts_to_payload(path, monkeypatch) -> bool:
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    return perm._SecretBox(str(path.parent)).unseal(path.read_bytes()) == _payload()


def _snapshot(d):
    return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file() and not p.is_symlink()}


class TestMigrationSafeguards:
    def test_the_backup_is_made_before_anything_is_encrypted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        legacy, original = _write_legacy(tmp_path)
        seen = {}

        real = perm._atomic_write

        def spy(path, data):
            seen["backups"] = [(p.name, p.read_bytes()) for p in tmp_path.glob("grants.jsonl.migrated-*")]
            seen["legacy_intact"] = legacy.read_bytes() == original
            return real(path, data)
        monkeypatch.setattr(perm, "_atomic_write", spy)
        GrantStore(str(tmp_path))
        assert len(seen["backups"]) == 1 and seen["backups"][0][1] == original and seen["legacy_intact"]

    def test_files_are_0600_and_the_directory_is_0700(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        os.chmod(tmp_path, 0o755)
        _write_legacy(tmp_path)
        GrantStore(str(tmp_path))
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)   # noqa: E731
        assert mode(tmp_path / "grants.enc") == 0o600 and mode(tmp_path) == 0o700
        assert all(mode(b) == 0o600 for b in tmp_path.glob("grants.jsonl.migrated-*"))

    def test_a_new_store_creates_a_private_directory_and_private_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        d = tmp_path / "fresh" / "perm"
        s = GrantStore(str(d))
        _add(s)
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)   # noqa: E731
        assert mode(d) == 0o700 and mode(d / "grants.enc") == 0o600 and mode(d / ".grants.lock") == 0o600

    def test_a_generated_key_file_is_0600(self, tmp_path):
        GrantStore(str(tmp_path), allow_generated_key_dev=True)
        assert stat.S_IMODE(os.stat(tmp_path / ".permissions.key").st_mode) == 0o600

    @pytest.mark.parametrize("env", [{"PERMISSIONS_REQUIRE_KEY": "1"}, {"PERMISSIONS_KEY": "not base64 !!"},
                                     {"PERMISSIONS_KEY": base64.b64encode(b"short").decode()}])
    def test_a_missing_or_bad_key_leaves_existing_data_byte_identical(self, tmp_path, monkeypatch, env):
        _write_legacy(tmp_path)
        before = _snapshot(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        with pytest.raises(RuntimeError):
            GrantStore(str(tmp_path))
        assert _snapshot(tmp_path) == before

    def test_no_cryptography_leaves_existing_data_byte_identical(self, tmp_path, monkeypatch):
        _write_legacy(tmp_path)
        before = _snapshot(tmp_path)
        monkeypatch.setattr(perm, "_HAVE_CRYPTO", False)
        with pytest.raises(RuntimeError):
            GrantStore(str(tmp_path))
        assert _snapshot(tmp_path) == before

    def test_a_wrong_key_leaves_an_existing_encrypted_store_untouched_and_sealed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _add(GrantStore(str(tmp_path)))
        before = _snapshot(tmp_path)
        monkeypatch.setenv("PERMISSIONS_KEY", base64.b64encode(b"x" * 32).decode())
        s = GrantStore(str(tmp_path))
        assert s.list() == [] and s.sealed and _snapshot(tmp_path) == before

    @pytest.mark.parametrize("crash", [Exception, _Crash])
    @pytest.mark.parametrize("step", ["backup", "fsync-1", "fsync-2", "fsync-3", "fsync-4", "replace", "verify"])
    def test_a_failure_at_any_step_leaves_the_old_file_or_the_verified_new_one(self, tmp_path, monkeypatch, step, crash):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        legacy, original = _write_legacy(tmp_path)
        boom = crash("injected")
        if step == "backup":
            monkeypatch.setattr(perm, "_copy_private", lambda *a, **k: (_ for _ in ()).throw(boom))
        elif step.startswith("fsync-"):
            n, real, count = int(step[-1]), os.fsync, [0]

            def fsync(fd):
                count[0] += 1
                if count[0] == n:
                    raise boom
                return real(fd)
            monkeypatch.setattr(os, "fsync", fsync)
        elif step == "replace":
            real = os.replace
            monkeypatch.setattr(os, "replace", lambda a, b, *k: (_ for _ in ()).throw(boom) if str(b).endswith("grants.enc") else real(a, b, *k))
        else:   # the encrypted file on disk does not decrypt to what was meant
            real_unseal, calls = perm._SecretBox.unseal, [0]

            def unseal(self, blob):
                calls[0] += 1
                if calls[0] == 2 and crash is not Exception:
                    raise boom
                return b"corrupt" if calls[0] == 2 else real_unseal(self, blob)
            monkeypatch.setattr(perm._SecretBox, "unseal", unseal)
        if crash is Exception:
            with pytest.raises(RuntimeError, match="left unchanged"):
                GrantStore(str(tmp_path))
            names = sorted(p.name for p in tmp_path.iterdir())
            assert legacy.read_bytes() == original and "grants.enc" not in names
            assert not any(n.endswith(".tmp") or n.startswith("grants.jsonl.migrated") for n in names)
        else:
            with pytest.raises(_Crash):
                GrantStore(str(tmp_path))
            monkeypatch.undo()
            enc = tmp_path / "grants.enc"
            assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
            if enc.exists():
                assert _decrypts_to_payload(enc, monkeypatch)            # never a partial encrypted file
            if legacy.exists():
                assert legacy.read_bytes() == original                  # never a partial plaintext file
            assert enc.exists() or legacy.exists()                      # the data never disappears

    def test_a_failure_removing_the_plaintext_original_leaves_both_consistent(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        legacy, original = _write_legacy(tmp_path)
        real = os.unlink
        monkeypatch.setattr(os, "unlink", lambda p, *a, **k: (_ for _ in ()).throw(OSError("busy")) if str(p).endswith("grants.jsonl") else real(p, *a, **k))
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            s = GrantStore(str(tmp_path))
        monkeypatch.undo()
        assert [g.id for g in s.list()] == ["g-s"] and legacy.read_bytes() == original
        assert _decrypts_to_payload(tmp_path / "grants.enc", monkeypatch)
        assert any("remove" in r.getMessage() for r in caplog.records)
        assert [g.id for g in GrantStore(str(tmp_path)).list()] == ["g-s"]     # the next start: enc wins, stale flagged

    def test_a_symlinked_plaintext_file_is_refused_and_its_target_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        target = tmp_path / "elsewhere"
        target.mkdir()
        real_file = target / "real.json"
        real_file.write_text(json.dumps(LEGACY_SECRET))
        d = tmp_path / "perm"
        d.mkdir()
        os.symlink(real_file, d / "grants.jsonl")
        with pytest.raises(RuntimeError, match="symbolic link"):
            GrantStore(str(d))
        assert json.loads(real_file.read_text()) == LEGACY_SECRET and not (d / "grants.enc").exists()
        assert [p.name for p in d.iterdir()] == ["grants.jsonl"]

    def test_a_symlink_where_the_encrypted_file_goes_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        _write_legacy(tmp_path)
        victim = tmp_path / "victim"
        os.symlink(victim, tmp_path / "grants.enc")              # dangling
        with pytest.raises(RuntimeError, match="symbolic link"):
            GrantStore(str(tmp_path))
        assert not victim.exists()

    def test_the_file_helpers_refuse_symlinks(self, tmp_path):
        src, dst, victim = tmp_path / "src", tmp_path / "dst", tmp_path / "victim"
        src.write_text("x")
        os.symlink(victim, dst)
        with pytest.raises(OSError):
            perm._copy_private(src, dst)                         # O_EXCL | O_NOFOLLOW
        with pytest.raises(RuntimeError, match="symbolic link"):
            perm._atomic_write(dst, b"x")
        os.symlink(src, tmp_path / "srclink")
        with pytest.raises(OSError):
            perm._read_regular(tmp_path / "srclink")
        assert not victim.exists()

    def test_a_symlinked_grants_file_is_not_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        s = GrantStore(str(tmp_path / "a"))
        _add(s)
        d = tmp_path / "b"
        d.mkdir()
        os.symlink(tmp_path / "a" / "grants.enc", d / "grants.enc")
        with pytest.raises(RuntimeError, match="symbolic link"):
            GrantStore(str(d))

    def test_logs_never_contain_keys_or_grant_contents(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        legacy, _ = _write_legacy(tmp_path)
        with caplog.at_level(logging.DEBUG):
            s = GrantStore(str(tmp_path))                        # migration
            s.add("app", SECRET_SUBJECT + "-2", "table", SECRET_RESOURCE, row_values=["zz-secret-value"])
            blob = bytearray((tmp_path / "grants.enc").read_bytes())
            blob[-1] ^= 1
            (tmp_path / "grants.enc").write_bytes(bytes(blob))
            s.list()                                             # sealed → error log
            (tmp_path / "bad").mkdir()
            (tmp_path / "bad" / "grants.jsonl").write_text("{ " + SECRET_SUBJECT)
            with pytest.raises(RuntimeError):
                GrantStore(str(tmp_path / "bad"))                # failed migration
        text = "\n".join(r.getMessage() for r in caplog.records)
        raw_key = base64.b64decode(KEY).hex()
        for needle in (KEY, raw_key, SECRET_SUBJECT, SECRET_RESOURCE, "zz-secret-value", "k" * 8):
            assert needle not in text

    def test_logs_never_contain_a_generated_key(self, tmp_path, caplog):
        with caplog.at_level(logging.DEBUG):
            GrantStore(str(tmp_path), allow_generated_key_dev=True)     # generates
            GrantStore(str(tmp_path))                                   # then reuses the existing file
        key_text = (tmp_path / ".permissions.key").read_text().strip()
        assert key_text and key_text not in "\n".join(r.getMessage() for r in caplog.records)

    def test_migration_can_still_be_switched_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        monkeypatch.setenv("PERMISSIONS_MIGRATE_PLAINTEXT", "0")
        _, original = _write_legacy(tmp_path)
        with pytest.raises(RuntimeError, match="PERMISSIONS_MIGRATE_PLAINTEXT"):
            GrantStore(str(tmp_path))
        assert _snapshot(tmp_path) == {"grants.jsonl": original, ".grants.lock": b""}


# ───────────────────────── the key requirement and the dev-only setting ─────────────────────────


class TestKeyRule:
    """(a) a valid PERMISSIONS_KEY; (b) an already existing valid key file; (c) otherwise fail closed —
    a key is never generated implicitly."""

    def test_c_no_key_and_no_key_file_fails_closed_and_touches_nothing(self, tmp_path):
        with pytest.raises(RuntimeError, match="PERMISSIONS_KEY") as e:
            GrantStore(str(tmp_path))
        assert "no existing key file" in str(e.value) and "PERMISSIONS_KEY_FILE" in str(e.value)
        assert list(tmp_path.iterdir()) == []                    # nothing generated, nothing created

    def test_c_the_error_names_the_two_dev_opt_ins_and_never_a_key(self, tmp_path):
        with pytest.raises(RuntimeError) as e:
            GrantStore(str(tmp_path))
        assert "PERMISSIONS_ALLOW_GENERATED_KEY_DEV" in str(e.value) and "PERMISSIONS_ALLOW_PLAINTEXT_DEV" in str(e.value)

    def test_c_a_key_is_never_generated_implicitly(self, tmp_path):
        d = tmp_path / "perm"
        with pytest.raises(RuntimeError):
            GrantStore(str(d))
        assert not (d / ".permissions.key").exists() and not d.exists()

    def test_a_a_valid_env_key_is_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        assert GrantStore(str(tmp_path)).status()["key_source"] == "env"
        assert not (tmp_path / ".permissions.key").exists()

    def test_b_an_existing_key_file_in_the_store_directory_keeps_working(self, tmp_path, caplog):
        _add(GrantStore(str(tmp_path), allow_generated_key_dev=True))     # an existing deployment's state
        before = (tmp_path / ".permissions.key").read_bytes()
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            s = GrantStore(str(tmp_path))                                  # no flag, no env key
        assert s.encrypted and s.status()["key_source"] == "keyfile" and [g.subject for g in s.list()] == ["support"]
        assert (tmp_path / ".permissions.key").read_bytes() == before      # never rewritten
        assert any("existing on-box key file" in r.getMessage() for r in caplog.records)

    def test_b_an_existing_key_file_elsewhere_via_permissions_key_file(self, tmp_path, monkeypatch):
        kdir = tmp_path / "keys"
        GrantStore(str(kdir), allow_generated_key_dev=True)
        monkeypatch.setenv("PERMISSIONS_KEY_FILE", str(kdir / ".permissions.key"))
        s = GrantStore(str(tmp_path / "data"))
        _add(s)
        assert s.encrypted and s.status()["key_source"] == "keyfile"

    def test_b_a_damaged_existing_key_file_is_an_error_and_is_not_replaced(self, tmp_path):
        (tmp_path / ".permissions.key").write_text("%%% not a key")
        with pytest.raises(RuntimeError, match="key file"):
            GrantStore(str(tmp_path), allow_generated_key_dev=True)       # even the dev opt-in must not overwrite it
        assert (tmp_path / ".permissions.key").read_text() == "%%% not a key"

    def test_dev_generated_key_is_loud_and_0600(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            s = GrantStore(str(tmp_path), allow_generated_key_dev=True)
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "DEVELOPMENT ONLY" in msg and "NOT FOR PRODUCTION" in msg and s.encrypted
        assert stat.S_IMODE(os.stat(tmp_path / ".permissions.key").st_mode) == 0o600

    def test_dev_generated_key_is_set_from_the_environment_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
        assert GrantStore(str(tmp_path)).status()["key_source"] == "keyfile"
        monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "0")
        with pytest.raises(RuntimeError):
            GrantStore(str(tmp_path / "other"))

    def test_dev_generated_key_that_cannot_be_written_fails_closed(self, tmp_path):
        (tmp_path / "afile").write_text("x")
        with pytest.raises(RuntimeError, match="generate"):
            GrantStore(str(tmp_path / "afile" / "d"), allow_generated_key_dev=True)

    def test_a_plaintext_opt_in_alone_still_means_plaintext_not_a_generated_key(self, tmp_path):
        s = GrantStore(str(tmp_path), allow_plaintext_dev=True)
        assert s.encrypted is False and not (tmp_path / ".permissions.key").exists()

    def test_require_key_refuses_even_an_existing_key_file(self, tmp_path, monkeypatch):
        GrantStore(str(tmp_path), allow_generated_key_dev=True)
        monkeypatch.setenv("PERMISSIONS_REQUIRE_KEY", "1")
        with pytest.raises(RuntimeError, match="PERMISSIONS_REQUIRE_KEY"):
            GrantStore(str(tmp_path))

    def test_require_key_passes_with_a_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_REQUIRE_KEY", "1")
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        assert GrantStore(str(tmp_path)).status()["key_source"] == "env"

    def test_a_plaintext_file_with_neither_key_nor_key_file_is_left_byte_identical(self, tmp_path):
        _write_legacy(tmp_path)
        before = _snapshot(tmp_path)
        with pytest.raises(RuntimeError, match="no existing key file"):
            GrantStore(str(tmp_path))
        assert _snapshot(tmp_path) == before

    def test_the_old_setting_name_no_longer_enables_plaintext(self, tmp_path, monkeypatch):
        monkeypatch.setattr(perm, "_HAVE_CRYPTO", False)
        monkeypatch.setenv("PERMISSIONS_ALLOW_PLAINTEXT", "1")
        with pytest.raises(RuntimeError):
            GrantStore(str(tmp_path))

    def test_the_dev_warning_is_loud(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(perm, "_HAVE_CRYPTO", False)
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            GrantStore(str(tmp_path), allow_plaintext_dev=True)
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "DEVELOPMENT ONLY" in msg and "NOT FOR PRODUCTION" in msg and "UNENCRYPTED" in msg

    def test_no_shipped_config_sets_the_dev_only_setting(self):
        """The dev-only settings must never arrive by default: no compose file, Dockerfile, env template,
        workflow or Makefile in the repository may set either (documentation may name them)."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        hits = []
        for pat in ("**/*.yml", "**/*.yaml", "**/Dockerfile*", "**/*.env", "**/.env*", "**/*.example", "**/Makefile",
                    "**/*.toml", "**/*.sh"):
            for f in root.glob(pat):
                if ".git" in f.parts or ".venv" in f.parts or "node_modules" in f.parts or not f.is_file():
                    continue
                try:
                    text = f.read_text(errors="ignore")
                    if "PERMISSIONS_ALLOW_PLAINTEXT_DEV" in text or "PERMISSIONS_ALLOW_GENERATED_KEY_DEV" in text:
                        hits.append(str(f.relative_to(root)))
                except OSError:
                    pass
        assert hits == []


class TestLockingNeedsAPlatformThatHasIt:
    def test_without_fcntl_the_store_refuses_to_start(self, tmp_path, monkeypatch):
        monkeypatch.setattr(perm, "_fcntl", None)
        with pytest.raises(RuntimeError, match="locking"):
            GrantStore(str(tmp_path))
