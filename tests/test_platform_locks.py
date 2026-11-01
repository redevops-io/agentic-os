"""The inter-process lock (``agentic_os._platform.locks``): flock on POSIX, LockFileEx on Windows. The same tests run on
Linux, macOS and Windows; what they prove is the contract every writer relies on: exclusive between handles AND between
processes, bounded wait, released by the holder or by its death, and no platform ever carries on unlocked."""
from __future__ import annotations

import os
import sys
import textwrap
import time

import pytest

from tests._proc_support import spawn
from agentic_os._platform import locks

_BIN = getattr(os, "O_BINARY", 0)


def _open(path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | _BIN, 0o600)


_HOLDER = textwrap.dedent("""
    import os, sys
    from agentic_os._platform import locks
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    locks.acquire(fd)
    print("held", flush=True)
    sys.stdin.readline()
    locks.release(fd)
    print("released", flush=True)
""")


def test_the_platform_has_a_lock():
    assert locks.available()


def test_a_second_handle_waits_then_gives_up_with_lock_timeout(tmp_path):
    a, b = _open(tmp_path / "l"), _open(tmp_path / "l")
    try:
        locks.acquire(a)
        t0 = time.monotonic()
        with pytest.raises(locks.LockTimeout):
            locks.acquire(b, timeout=0.3)
        assert 0.25 <= time.monotonic() - t0 < 5
        with pytest.raises(locks.LockTimeout):
            locks.acquire(b, timeout=0)               # zero tries exactly once
        locks.release(a)
        locks.acquire(b, timeout=2)                   # and it is free as soon as the holder lets go
        locks.release(b)
    finally:
        os.close(a)
        os.close(b)


def test_locked_accepts_a_file_object_and_releases_on_error(tmp_path):
    p = tmp_path / "log"
    with open(p, "ab") as fh:
        with pytest.raises(RuntimeError):
            with locks.locked(fh):
                raise RuntimeError("boom")
        with open(p, "ab") as other, locks.locked(other, timeout=1):   # the failed block did not leave it held
            pass


def test_file_lock_creates_the_file_and_is_exclusive(tmp_path):
    p = tmp_path / "sub.lock"
    with locks.file_lock(p) as fd:
        assert os.path.exists(p) and isinstance(fd, int)
        with pytest.raises(locks.LockTimeout):
            with locks.file_lock(p, timeout=0.2):
                pass
    with locks.file_lock(p, timeout=1):
        pass


def test_a_lock_held_by_another_process_is_respected_and_released(tmp_path):
    p = tmp_path / "x.lock"
    child = spawn(_HOLDER, str(p))
    try:
        assert child.stdout.readline().strip() == "held", child.stderr.read()
        fd = _open(p)
        try:
            with pytest.raises(locks.LockTimeout):
                locks.acquire(fd, timeout=0.3)
            child.stdin.write("go\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "released"
            locks.acquire(fd, timeout=5)
            locks.release(fd)
        finally:
            os.close(fd)
    finally:
        child.kill()
        child.wait()


def test_a_lock_is_released_when_its_holder_dies(tmp_path):
    p = tmp_path / "dead.lock"
    child = spawn(_HOLDER, str(p))
    try:
        assert child.stdout.readline().strip() == "held", child.stderr.read()
    finally:
        child.kill()
        child.wait()
    fd = _open(p)
    try:
        locks.acquire(fd, timeout=10)                  # a crashed writer never wedges the store
        locks.release(fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW exists on POSIX only; on Windows a link is refused where the private-file check applies (see test_platform_files.py)")
def test_file_lock_can_refuse_a_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"")
    link = tmp_path / "link"
    os.symlink(target, link)
    with pytest.raises(OSError):
        with locks.file_lock(link, no_follow=True):
            pass


def test_a_platform_with_no_lock_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "_fcntl", None)
    monkeypatch.setattr(locks, "_WINDOWS", False)
    assert not locks.available()
    fd = _open(tmp_path / "n")
    try:
        with pytest.raises(locks.LockUnavailable):
            locks.acquire(fd)
    finally:
        os.close(fd)


def test_the_event_log_refuses_to_append_without_a_lock(tmp_path, monkeypatch):
    from agentic_os.mission.store import EventStore
    store = EventStore(str(tmp_path / "log.jsonl"))
    store.append("A", "m1", {"n": 1})
    monkeypatch.setattr(locks, "_fcntl", None)
    monkeypatch.setattr(locks, "_WINDOWS", False)
    with pytest.raises(locks.LockUnavailable):
        store.append("A", "m1", {"n": 2})
    assert [e.payload["n"] for e in store.all()] == [1]


def test_the_grant_store_refuses_to_start_without_a_lock(tmp_path, monkeypatch):
    from agentic_os.permissions import GrantStorageError, GrantStore
    monkeypatch.setattr(locks, "_fcntl", None)
    monkeypatch.setattr(locks, "_WINDOWS", False)
    with pytest.raises(GrantStorageError, match="locking"):
        GrantStore(str(tmp_path / "g"))


_APPENDER = textwrap.dedent("""
    import sys
    from agentic_os.mission.store import EventStore
    path, who, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    s = EventStore(path)
    for i in range(n):
        s.append("Tick", "shared" if i % 2 else "own-" + who, {"who": who, "i": i})
""")


def test_concurrent_processes_extend_one_hash_chain_without_forking_it(tmp_path):
    from agentic_os.mission.store import EventStore, verify_chain
    path = str(tmp_path / "chain.jsonl")
    procs = [spawn(_APPENDER, path, f"w{k}", "30", AGENTIC_OS_EVENT_FSYNC="0") for k in range(4)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err[-800:]
    events = EventStore(path).all()
    assert len(events) == 120
    assert sorted(e.seq for e in events) == list(range(1, 121))        # one global sequence: nobody extended a stale head
    report = verify_chain(events)
    assert report.ok and report.checked == 120 and report.unchained == 0, report
    raw = open(path, "rb").read()
    assert b"\r" not in raw and raw.endswith(b"\n")                     # LF-only on every OS


def test_concurrent_processes_do_not_duplicate_outcome_events(tmp_path):
    from agentic_os.outcome_store import FileOutcomeStore
    path = str(tmp_path / "outcomes.jsonl")
    code = textwrap.dedent("""
        import sys
        from agentic_os.outcome_store import FileOutcomeStore
        from agentic_os.priority_engine import OutcomeEvent
        s = FileOutcomeStore(sys.argv[1])
        for i in range(20):
            s.append(OutcomeEvent(candidate_id="c%d" % i, source_app="app", accepted=True, event_id="e%d" % i))
    """)
    procs = [spawn(code, path) for _ in range(3)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err[-800:]
    events = FileOutcomeStore(path).load()
    assert sorted(e.candidate_id for e in events) == sorted("c%d" % i for i in range(20))   # idempotent on the event id across processes
    assert b"\r" not in open(path, "rb").read()


# ── the Windows wait policy: interruptible, bounded, fair (POSIX keeps the blocking flock, which the kernel already makes interruptible) ──

_windows_only = pytest.mark.skipif(os.name != "nt", reason="the Windows wait policy (queue byte plus bounded polling); POSIX waits in flock")


@_windows_only
def test_an_unbounded_wait_is_bounded_and_ends_in_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "_WINDOWS_DEFAULT_WAIT_S", 0.5)
    holder, waiter = _open(tmp_path / "b.lock"), _open(tmp_path / "b.lock")
    try:
        locks.acquire(holder)
        t0 = time.monotonic()
        with pytest.raises(locks.LockTimeout, match="bounded"):
            locks.acquire(waiter)                       # no timeout given: the Windows default bound applies
        assert 0.4 <= time.monotonic() - t0 < 10
        locks.release(holder)
        locks.acquire(waiter, timeout=2)                # a timed-out waiter left nothing held
        locks.release(waiter)
    finally:
        os.close(holder)
        os.close(waiter)


@_windows_only
def test_a_waiting_acquire_can_be_interrupted_and_leaves_nothing_held(tmp_path):
    import ctypes
    import threading
    p = tmp_path / "i.lock"
    holder = _open(p)
    locks.acquire(holder)
    result = {}

    def waiter():
        fd = _open(p)
        try:
            locks.acquire(fd)
        except BaseException as e:                      # noqa: BLE001 - the interrupt is what is being observed
            result["exc"] = e
        finally:
            os.close(fd)
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.3)                                     # the waiter is now waiting
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident), ctypes.py_object(KeyboardInterrupt))
    t.join(5)
    still_waiting = t.is_alive()
    locks.release(holder)                               # never leave the helper thread blocked, whatever the outcome
    t.join(10)
    os.close(holder)
    assert not still_waiting and isinstance(result.get("exc"), KeyboardInterrupt)
    fd = _open(p)
    try:
        locks.acquire(fd, timeout=2)                    # the interrupted waiter released its place in the queue
        locks.release(fd)
    finally:
        os.close(fd)


@_windows_only
def test_a_holder_that_retakes_the_lock_in_a_loop_cannot_starve_a_waiter(tmp_path):
    """The holder keeps the lock for 20 ms, releases it and asks again at once, until the waiter has it. Acquirers take a queue slot first, so
    the holder cannot get back in ahead of a waiter that is already waiting: the waiter gets the lock at the first release, within a bound
    far above that (5 s) so a slow runner cannot make this flaky."""
    import threading
    p = tmp_path / "f.lock"
    got, started = threading.Event(), threading.Event()
    rounds = []

    def holder():
        fd = _open(p)
        try:
            end = time.monotonic() + 30
            while not got.is_set() and time.monotonic() < end:
                locks.acquire(fd, timeout=20)
                started.set()
                time.sleep(0.02)
                locks.release(fd)
                rounds.append(1)
        finally:
            os.close(fd)
    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(10)
    fd = _open(p)
    try:
        t0 = time.monotonic()
        locks.acquire(fd, timeout=20)
        waited = time.monotonic() - t0
        got.set()
        locks.release(fd)
    finally:
        os.close(fd)
        got.set()
        t.join(30)
    assert waited < 5, f"the waiter waited {waited:.2f} s through {len(rounds)} holder rounds"


@_windows_only
def test_the_event_log_ends_a_stuck_lock_wait_in_a_clear_error(tmp_path, monkeypatch):
    from agentic_os.mission.store import EventStore
    monkeypatch.setattr(locks, "_WINDOWS_DEFAULT_WAIT_S", 0.5)
    log = tmp_path / "log.jsonl"
    store = EventStore(str(log))
    store.append("A", "m1", {"n": 1})
    holder = _open(log)
    try:
        locks.acquire(holder)
        with pytest.raises(locks.LockTimeout, match="bounded"):
            store.append("A", "m1", {"n": 2})
    finally:
        locks.release(holder)
        os.close(holder)
    assert [e.payload["n"] for e in EventStore(str(log)).all()] == [1]
