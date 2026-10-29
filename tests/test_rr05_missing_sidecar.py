"""RR-05: a MISSING rollback sidecar next to an existing non-empty store fails closed.

Before: ``grants.counter`` missing counted as floor 0, so restoring an older ``grants.enc`` AND deleting the sidecar was accepted silently.
Now a store whose own authenticated counter is above 1 and whose sidecar is missing is refused (sealed, no grant honoured, writes refused)
with the exact recovery command ``agentic-os permissions recover-counter --confirm``. Kept, stated: a whole-volume restore carries the
sidecar with it; the recommended deployment keeps the sidecar OFF the data volume (``PERMISSIONS_COUNTER_FILE``).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from agentic_os.permissions import GrantStorageError, GrantStore, Identity, PermissionsPlane

KEY = base64.b64encode(b"m" * 32).decode()
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    monkeypatch.delenv("PERMISSIONS_COUNTER_FILE", raising=False)
    return GrantStore(str(tmp_path / "p"))


def _side(s):
    return pathlib.Path(s.dir) / "grants.counter"


def _cli(*args, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    e.update(PYTHONPATH=str(ROOT), **env)
    return subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", *args], env=e, cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)


def _two_versions(store):
    """An older valid store (counter 2) and the current one (counter 4); the current one is in place."""
    store.add("role", "clerk", "table", "t")
    g = store.add("role", "temp", "table", "t")
    old = store.path.read_bytes()
    store.remove(g.id)
    store.add("role", "other", "table", "t")
    return old


class TestMissingSidecar:
    def test_the_demonstrated_bypass_is_closed_old_store_plus_deleted_sidecar_is_refused(self, store, caplog):
        old = _two_versions(store)
        store.path.write_bytes(old)                      # restore the older store ...
        _side(store).unlink()                            # ... and delete the sidecar that would have revealed it
        plane = PermissionsPlane(store)
        with caplog.at_level(logging.ERROR, logger="agentic_os.permissions"):
            d = plane.authorize(Identity(roles=["temp"]), "table", "t")
        assert d.allowed is False and "sealed" in d.reason                 # the revoked grant is NOT reinstated
        st = store.status()
        assert st["sealed"] is True and st["counter_missing"] is True
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "agentic-os permissions recover-counter" in text and "--confirm" in text
        with pytest.raises(RuntimeError, match="sealed"):
            store.add("role", "x", "table", "t")

    def test_a_current_store_whose_sidecar_was_deleted_is_refused_too(self, store):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        _side(store).unlink()
        assert store.status()["sealed"] is True and store.list() == []

    def test_the_first_write_is_tolerated_a_crash_between_payload_and_sidecar_must_not_lock_a_fresh_store(self, store):
        store.add("role", "a", "table", "t")             # counter 1
        _side(store).unlink()
        st = store.status()
        assert st["sealed"] is False and st["counter_missing"] is False
        store.add("role", "b", "table", "t")             # the next write records the sidecar again
        assert _side(store).exists()

    def test_a_store_written_by_a_release_without_a_counter_is_still_accepted_without_a_sidecar(self, store):
        store.add("role", "a", "table", "t")
        doc = json.loads(store._box.unseal(store.path.read_bytes()))
        doc.pop("meta")
        store.path.write_bytes(store._box.seal(json.dumps(doc).encode()))
        _side(store).unlink()
        assert store.status()["sealed"] is False

    def test_an_empty_or_absent_store_is_unaffected(self, store):
        assert store.status()["sealed"] is False and store.status()["counter_missing"] is False
        store.seed([])
        assert store.status()["sealed"] is False

    def test_a_symlinked_sidecar_is_refused_so_it_cannot_lower_the_floor(self, store, tmp_path):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        low = tmp_path / "low"
        low.write_text("0\n")
        _side(store).unlink()
        os.symlink(low, _side(store))
        assert store.status()["sealed"] is True


class TestRecoverCounter:
    def test_without_the_confirmation_flag_it_changes_nothing(self, store):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        _side(store).unlink()
        r = _cli("recover-counter", "--dir", store.dir, PERMISSIONS_KEY=KEY)
        assert r.returncode == 2 and "--confirm" in r.stderr + r.stdout
        assert not _side(store).exists() and store.status()["sealed"] is True

    def test_with_the_flag_it_verifies_the_store_and_re_establishes_the_counter(self, store):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        want = store.status()["counter"]
        _side(store).unlink()
        assert store.status()["sealed"] is True
        r = _cli("recover-counter", "--dir", store.dir, "--confirm", PERMISSIONS_KEY=KEY)
        assert r.returncode == 0 and "recorded" in r.stdout
        assert int(_side(store).read_text()) == want and store.status()["sealed"] is False
        assert sorted(g.subject for g in store.list()) == ["a", "b"]
        store.add("role", "c", "table", "t")

    def test_it_refuses_when_the_store_does_not_open(self, store):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        _side(store).unlink()
        r = _cli("recover-counter", "--dir", store.dir, "--confirm", PERMISSIONS_KEY=base64.b64encode(b"z" * 32).decode())
        assert r.returncode == 2 and not _side(store).exists()

    def test_it_refuses_when_a_sidecar_exists_that_is_the_rollback_case_with_its_own_command(self, store):
        store.add("role", "a", "table", "t")
        r = _cli("recover-counter", "--dir", store.dir, "--confirm", PERMISSIONS_KEY=KEY)
        assert r.returncode == 2 and "accept-rollback" in r.stderr + r.stdout

    def test_the_api_method_mirrors_the_command(self, store):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        _side(store).unlink()
        assert store.recover_counter() == 2
        with pytest.raises(GrantStorageError):
            store.recover_counter()                                         # now the sidecar exists


class TestOffVolumeSidecar:
    """The recommended deployment: the counter lives on a different volume from the data."""

    def test_a_whole_data_directory_restore_is_detected_when_the_sidecar_is_off_volume(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        monkeypatch.setenv("PERMISSIONS_COUNTER_FILE", str(tmp_path / "other-volume" / "counter"))
        s = GrantStore(str(tmp_path / "data"))
        s.add("role", "clerk", "table", "t")
        snapshot = tmp_path / "snap"
        shutil.copytree(s.dir, snapshot)                                   # a volume snapshot of the DATA directory
        s.add("role", "later", "table", "t")
        shutil.rmtree(s.dir)
        shutil.copytree(snapshot, s.dir)                                   # restored
        again = GrantStore(s.dir)
        assert again.status()["sealed"] is True and again.status()["rollback_detected"] is True

    def test_documented_residual_a_whole_directory_restore_with_the_default_sidecar_is_not_detected(self, tmp_path, monkeypatch):
        """Stated, not hidden: the default sidecar sits next to the data, so a restore of the whole directory restores both."""
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        monkeypatch.delenv("PERMISSIONS_COUNTER_FILE", raising=False)
        s = GrantStore(str(tmp_path / "data"))
        s.add("role", "clerk", "table", "t")
        snapshot = tmp_path / "snap"
        shutil.copytree(s.dir, snapshot)
        g = s.add("role", "later", "table", "t")
        s.remove(g.id)
        shutil.rmtree(s.dir)
        shutil.copytree(snapshot, s.dir)
        assert GrantStore(s.dir).status()["sealed"] is False

    def test_adopting_an_off_volume_sidecar_on_an_existing_store_needs_the_explicit_recovery_step(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        monkeypatch.delenv("PERMISSIONS_COUNTER_FILE", raising=False)
        s = GrantStore(str(tmp_path / "data"))
        s.add("role", "a", "table", "t")
        s.add("role", "b", "table", "t")
        side = tmp_path / "other-volume" / "counter"
        monkeypatch.setenv("PERMISSIONS_COUNTER_FILE", str(side))
        moved = GrantStore(s.dir)
        assert moved.status()["sealed"] is True and moved.status()["counter_missing"] is True
        r = _cli("recover-counter", "--dir", s.dir, "--confirm", PERMISSIONS_KEY=KEY, PERMISSIONS_COUNTER_FILE=str(side))
        assert r.returncode == 0 and int(side.read_text()) == 2
        assert GrantStore(s.dir).status()["sealed"] is False


def test_the_migration_guide_documents_the_missing_sidecar_rule_the_command_and_the_off_volume_placement():
    t = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    assert "recover-counter" in t and "PERMISSIONS_COUNTER_FILE" in t and "off the data volume" in t.lower()


class TestConcurrentWriterIsNotARollback:
    """A writer replaces the payload and THEN the sidecar: a lock-free reader that reads the payload and then a sidecar a concurrent writer has
    already advanced must not call that a rollback (it made test_processes_lose_nothing flaky). A restored OLD file is still refused."""

    def test_a_transient_inconsistency_is_re_read_and_accepted(self, store, monkeypatch):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        real = store._counter_floor
        calls = []

        def racing():
            calls.append(1)
            return real() + 1 if len(calls) == 1 else real()           # the first read sees a sidecar one ahead, as if a writer just advanced it
        monkeypatch.setattr(store, "_counter_floor", racing)
        st = store.status()
        assert st["sealed"] is False and st["rollback_detected"] is False and len(calls) >= 2

    def test_a_persistent_inconsistency_is_a_rollback_and_is_logged_once(self, store, monkeypatch, caplog):
        store.add("role", "a", "table", "t")
        store.add("role", "b", "table", "t")
        real = store._counter_floor
        monkeypatch.setattr(store, "_counter_floor", lambda: real() + 1)
        with caplog.at_level(logging.ERROR, logger="agentic_os.permissions"):
            st = store.status()
        assert st["sealed"] is True and st["rollback_detected"] is True
        assert sum("is OLDER" in r.getMessage() for r in caplog.records) == 1
