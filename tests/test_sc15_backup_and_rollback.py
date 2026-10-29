"""SC-15: (a) the plaintext migration backup warns on every start while it exists and has an explicit, verified, overwrite-then-remove
command; (b) rollback protection for grants.enc: a monotonically increasing counter inside the AUTHENTICATED payload, checked against a
separate small sidecar file; an older store is refused (fail closed) with a clear message and an explicit recovery command."""
from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import threading

import pytest

from agentic_os.permissions import GrantStorageError, GrantStore, Identity, PermissionsPlane

KEY = base64.b64encode(b"b" * 32).decode()
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _legacy_file(d):
    (d / "grants.jsonl").write_text(json.dumps({"version": 1, "grants": [{
        "id": "g-old", "subject_kind": "role", "subject": "zz-secret-subject", "resource_kind": "table", "resource_name": "t",
        "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [], "created_by": "o",
        "at": 1.0}]}))


@pytest.fixture()
def migrated(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    d = tmp_path / "p"
    d.mkdir()
    _legacy_file(d)
    return GrantStore(str(d))


def _cli(*args, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    e.update(PYTHONPATH=str(ROOT), **env)
    return subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", *args], env=e, cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)


class TestPlaintextBackup:
    def test_every_start_warns_while_the_backup_exists(self, migrated, caplog):
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            GrantStore(migrated.dir)
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "PLAINTEXT" in text and "agentic-os permissions migrate --purge-backup" in text and "zz-secret" not in text

    def test_purge_overwrites_in_place_then_removes_after_verifying_the_encrypted_store(self, migrated):
        backup = next(pathlib.Path(migrated.dir).glob("grants.jsonl.migrated-*"))
        size = backup.stat().st_size
        alias = pathlib.Path(migrated.dir) / "alias"
        os.link(backup, alias)                         # a second name for the same inode: proves the bytes were overwritten in place
        assert b"zz-secret-subject" in alias.read_bytes()
        assert migrated.purge_migration_backups() == 1
        assert not backup.exists() and alias.read_bytes() == b"\x00" * size
        assert migrated.purge_migration_backups() == 0

    def test_no_warning_after_the_purge(self, migrated, caplog):
        migrated.purge_migration_backups()
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            GrantStore(migrated.dir)
        assert not any("PLAINTEXT migration backup" in r.getMessage() for r in caplog.records)

    def test_it_refuses_when_the_encrypted_store_does_not_open(self, migrated):
        backup = next(pathlib.Path(migrated.dir).glob("grants.jsonl.migrated-*"))
        before = backup.read_bytes()
        migrated.path.write_bytes(b"garbage" * 20)
        with pytest.raises(GrantStorageError, match="does not open"):
            migrated.purge_migration_backups()
        assert backup.read_bytes() == before

    def test_it_never_follows_a_symlink(self, migrated, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("precious")
        link = pathlib.Path(migrated.dir) / "grants.jsonl.migrated-1-link"
        os.symlink(victim, link)
        migrated.purge_migration_backups()
        assert victim.read_text() == "precious"

    def test_the_cli_command(self, migrated):
        r = _cli("migrate", "--purge-backup", "--dir", migrated.dir, PERMISSIONS_KEY=KEY)
        assert r.returncode == 0 and "purged 1" in r.stdout and not list(pathlib.Path(migrated.dir).glob("grants.jsonl.migrated-*"))
        assert _cli("migrate", "--dir", migrated.dir, PERMISSIONS_KEY=KEY).returncode == 2        # the flag is required


class TestRollbackProtection:
    @pytest.fixture()
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        monkeypatch.delenv("PERMISSIONS_COUNTER_FILE", raising=False)
        (tmp_path / "p").mkdir()
        return GrantStore(str(tmp_path / "p"))

    def _counter(self, s):
        return json.loads(s._box.unseal(s.path.read_bytes())).get("meta", {}).get("counter", 0)

    def test_the_counter_increases_with_every_write_inside_the_authenticated_payload(self, store):
        store.add("role", "a", "table", "t")
        c1 = self._counter(store)
        g = store.add("role", "b", "table", "t")
        store.update(g.id, masked_columns=["x"])
        store.remove(g.id)
        assert self._counter(store) == c1 + 3
        raw = store.path.read_bytes()
        assert b"counter" not in raw                      # inside the encrypted payload, not visible

    def test_a_sidecar_records_the_highest_counter_with_mode_0600(self, store):
        store.add("role", "a", "table", "t")
        side = pathlib.Path(store.dir) / "grants.counter"
        assert int(side.read_text()) == self._counter(store) and stat.S_IMODE(os.stat(side).st_mode) == 0o600

    def test_restoring_an_older_valid_store_is_refused_fail_closed_with_the_recovery_command(self, store, caplog):
        g = store.add("role", "clerk", "table", "t")
        old = store.path.read_bytes()                     # a backup, valid and encrypted with the right key
        store.remove(g.id)                                # the grant is revoked
        shutil.copyfile(store.path, str(store.path) + ".new")
        store.path.write_bytes(old)                       # ... and the old file is restored
        plane = PermissionsPlane(store)
        with caplog.at_level(logging.ERROR, logger="agentic_os.permissions"):
            d = plane.authorize(Identity(roles=["clerk"]), "table", "t")
        assert d.allowed is False and "sealed" in d.reason          # the revoked grant is NOT reinstated
        st = store.status()
        assert st["sealed"] is True and st["rollback_detected"] is True
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "older" in text and "agentic-os permissions accept-rollback" in text
        with pytest.raises(RuntimeError, match="sealed"):
            store.add("role", "x", "table", "t")                    # writes are refused too

    def test_the_explicit_recovery_command_accepts_the_older_store(self, store):
        g = store.add("role", "clerk", "table", "t")
        old = store.path.read_bytes()
        store.remove(g.id)
        store.path.write_bytes(old)
        assert store.status()["sealed"] is True
        r = _cli("accept-rollback", "--dir", store.dir, PERMISSIONS_KEY=KEY)
        assert r.returncode == 0 and "accepted" in r.stdout
        assert store.status()["sealed"] is False and [x.subject for x in store.list()] == ["clerk"]
        store.add("role", "after", "table", "t")                     # and it is writable again, counter moving on

    def test_a_missing_sidecar_is_accepted_on_upgrade_and_created_on_the_next_write(self, store):
        store.add("role", "a", "table", "t")
        (pathlib.Path(store.dir) / "grants.counter").unlink()
        assert store.status()["sealed"] is False
        store.add("role", "b", "table", "t")
        assert (pathlib.Path(store.dir) / "grants.counter").exists()

    def test_a_garbled_sidecar_fails_closed(self, store):
        store.add("role", "a", "table", "t")
        (pathlib.Path(store.dir) / "grants.counter").write_text("not a number")
        assert store.status()["sealed"] is True

    def test_a_store_written_without_a_counter_by_an_older_release_is_refused_once_a_counter_was_recorded(self, store):
        store.add("role", "a", "table", "t")
        doc = json.loads(store._box.unseal(store.path.read_bytes()))
        doc.pop("meta")                                   # what a release that predates the counter would have written
        store.path.write_bytes(store._box.seal(json.dumps(doc).encode()))
        assert store.status()["rollback_detected"] is True

    def test_the_sidecar_can_live_outside_the_data_volume(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        side = tmp_path / "elsewhere" / "counter"
        monkeypatch.setenv("PERMISSIONS_COUNTER_FILE", str(side))
        s = GrantStore(str(tmp_path / "p"))
        s.add("role", "a", "table", "t")
        assert side.exists() and not (tmp_path / "p" / "grants.counter").exists()

    def test_a_wrong_key_is_still_just_sealed_not_a_rollback(self, store, monkeypatch):
        store.add("role", "a", "table", "t")
        monkeypatch.setenv("PERMISSIONS_KEY", base64.b64encode(b"z" * 32).decode())
        other = GrantStore(store.dir)
        assert other.status()["sealed"] is True and other.status()["rollback_detected"] is False

    def test_concurrent_writers_keep_the_counter_strictly_increasing(self, store):
        ts = [threading.Thread(target=lambda i=i: store.add("role", f"r{i}", "table", "t")) for i in range(12)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert self._counter(store) == 12 and len(store.list()) == 12 and store.status()["sealed"] is False
