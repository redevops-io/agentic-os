"""SC-05 (data loss) and SC-13 (unbounded lock wait), from the 2026-10 AI-assisted permissions security review.

SC-05: the sealed/unsealed state of a read is per OPERATION, derived inside the locked transaction; a shared flag another thread can
clear must never let a writer overwrite the store after its own read failed. A failed read inside a write transaction aborts the write.
SC-13: the grants lock has a bounded wait with a clear error; the error leaves the data intact.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import os
import threading
import time

import pytest

from agentic_os import permissions as P
from agentic_os.permissions import GrantStorageError, GrantStore, Identity, PermissionsPlane

KEY = base64.b64encode(b"s" * 32).decode()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    s = GrantStore(str(tmp_path / "p"))
    for i in range(5):
        s.add("user", f"u{i}", "table", "x")
    return s


def _subjects(s):
    return sorted(g.subject for g in s.list())


class TestSC05TheReadStateIsPerOperation:
    def _interleave(self, store, monkeypatch, op):
        """A forced interleaving: the writer's own read fails once (a transient I/O error), and in the window before it looks
        at the result a different thread completes a successful read."""
        real_read, state = P._read_regular, {"first": True}

        def flaky_read(path):
            if threading.current_thread().name == "writer" and state["first"]:
                state["first"] = False
                raise OSError(errno.EMFILE, "too many open files")
            return real_read(path)

        real_log = P._log

        class Log:
            def __getattr__(self, n):
                return getattr(real_log, n)

            def error(self, *a, **k):
                if threading.current_thread().name == "writer":
                    t = threading.Thread(target=store.list, name="reader")
                    t.start()
                    t.join()
                return real_log.error(*a, **k)
        monkeypatch.setattr(P, "_read_regular", flaky_read)
        monkeypatch.setattr(P, "_log", Log())
        out = {}

        def run():
            try:
                op(out)
            except Exception as e:  # noqa: BLE001
                out["error"] = e
        w = threading.Thread(target=run, name="writer")
        w.start()
        w.join()
        monkeypatch.undo()
        return out

    def test_a_writer_whose_own_read_failed_never_overwrites_the_store(self, store, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        before = _subjects(store)
        out = self._interleave(store, monkeypatch, lambda o: o.setdefault("g", store.add("user", "new", "table", "x")))
        assert "error" in out and "g" not in out            # the add aborted ...
        assert _subjects(store) == before                    # ... and nothing was destroyed (before the fix the store held only ['new'])

    @pytest.mark.parametrize("name", ["remove", "update", "seed"])
    def test_remove_update_and_seed_abort_too(self, store, monkeypatch, name):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        gid = store.list()[0].id
        ops = {"remove": lambda o: o.setdefault("r", store.remove(gid)),
               "update": lambda o: o.setdefault("u", store.update(gid, masked_columns=["email"])),
               "seed": lambda o: store.seed([{"subject_kind": "user", "subject": "s", "resource_kind": "table", "resource_name": "x"}])}
        before = _subjects(store)
        out = self._interleave(store, monkeypatch, ops[name])
        assert "error" in out and _subjects(store) == before

    def test_the_shared_flag_is_informational_only_a_cleared_flag_cannot_authorise_a_write(self, store, monkeypatch):
        real = P._read_regular
        monkeypatch.setattr(P, "_read_regular", lambda p: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
        store.sealed = False                                   # someone cleared it
        with pytest.raises(RuntimeError, match="sealed"):
            store.add("user", "new", "table", "x")
        monkeypatch.setattr(P, "_read_regular", real)
        assert len(store.list()) == 5

    def test_a_concurrent_failed_read_does_not_taint_another_readers_decision(self, store, monkeypatch):
        real, seen = P._read_regular, {}
        plane = PermissionsPlane(store)
        store.add("user", "ana", "table", "x")

        def flaky(path):
            if threading.current_thread().name == "bad":
                raise OSError(errno.EIO, "io")
            return real(path)
        monkeypatch.setattr(P, "_read_regular", flaky)

        def bad():
            seen["bad"] = store.snapshot()
        t = threading.Thread(target=bad, name="bad")
        t.start()
        t.join()
        good = plane.authorize(Identity(user="ana"), "table", "x")
        assert seen["bad"][1] is True and good.allowed is True     # the flag a failed read left behind does not deny this reader

    def test_snapshot_reports_sealed_per_call(self, store):
        grants, sealed = store.snapshot()
        assert len(grants) == 5 and sealed is False
        store.path.write_bytes(b"garbage" * 10)
        grants, sealed = store.snapshot()
        assert grants == [] and sealed is True


def _hold_lock(directory, started, release):
    fd = os.open(os.path.join(directory, ".grants.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    started.set()
    release.wait(30)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


class TestSC13TheLockHasABoundedWait:
    def _held(self, store):
        started, release = threading.Event(), threading.Event()
        t = threading.Thread(target=_hold_lock, args=(store.dir, started, release))
        t.start()
        assert started.wait(5)
        return release, t

    def test_a_writer_gives_up_with_a_clear_error_and_leaves_the_data_intact(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        s = GrantStore(str(tmp_path / "p"), lock_timeout=0.4)
        s.add("user", "keep", "table", "x")
        release, t = self._held(s)
        t0 = time.monotonic()
        try:
            with pytest.raises(GrantStorageError, match="lock") as e:
                s.add("user", "new", "table", "x")
            assert 0.3 <= time.monotonic() - t0 < 5 and "0.4" in str(e.value) and "data was not changed" in str(e.value)
            for call in (lambda: s.remove("g-" + "0" * 32), lambda: s.update("g-" + "0" * 32, masked_columns=["a"]),
                         lambda: s.seed([])):
                with pytest.raises(GrantStorageError, match="lock"):
                    call()
        finally:
            release.set()
            t.join(10)
        assert _subjects(s) == ["keep"]
        s.add("user", "after", "table", "x")                       # and it works once the holder is gone
        assert _subjects(s) == ["after", "keep"]

    def test_reads_and_decisions_never_wait_for_the_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        s = GrantStore(str(tmp_path / "p"), lock_timeout=0.3)
        s.add("user", "ana", "table", "x")
        release, t = self._held(s)
        try:
            t0 = time.monotonic()
            assert PermissionsPlane(s).authorize(Identity(user="ana"), "table", "x").allowed
            assert time.monotonic() - t0 < 0.3
        finally:
            release.set()
            t.join(10)

    def test_the_default_is_ten_seconds_and_it_is_configurable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        assert GrantStore(str(tmp_path / "a")).lock_timeout == 10.0
        monkeypatch.setenv("PERMISSIONS_LOCK_TIMEOUT", "2.5")
        assert GrantStore(str(tmp_path / "b")).lock_timeout == 2.5
        assert GrantStore(str(tmp_path / "c"), lock_timeout=7).lock_timeout == 7.0

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", "nan", "inf", ""])
    def test_a_bad_timeout_setting_is_refused_never_an_infinite_wait(self, tmp_path, monkeypatch, bad):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        monkeypatch.setenv("PERMISSIONS_LOCK_TIMEOUT", bad)
        if bad == "":
            assert GrantStore(str(tmp_path / "p")).lock_timeout == 10.0      # unset-like: the default
        else:
            with pytest.raises(GrantStorageError, match="PERMISSIONS_LOCK_TIMEOUT"):
                GrantStore(str(tmp_path / "p"))

    def test_start_up_migration_also_stops_at_the_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        d = tmp_path / "p"
        d.mkdir()
        (d / "grants.jsonl").write_text('{"version": 1, "grants": []}')
        before = (d / "grants.jsonl").read_bytes()
        started, release = threading.Event(), threading.Event()
        t = threading.Thread(target=_hold_lock, args=(str(d), started, release))
        t.start()
        started.wait(5)
        try:
            with pytest.raises(GrantStorageError, match="lock"):
                GrantStore(str(d), lock_timeout=0.3)
        finally:
            release.set()
            t.join(10)
        assert (d / "grants.jsonl").read_bytes() == before and not (d / "grants.enc").exists()

    def test_the_mutation_routes_do_not_block_the_event_loop(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        for f in ("agentic_os/control_plane.py", "deploy/permissions-showcase-demo/app.py"):
            text = (root / f).read_text()
            assert "run_in_threadpool" in text, f
