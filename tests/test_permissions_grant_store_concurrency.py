"""The grant store loses no update and never tears its file under concurrent writers.

One lock covers the whole read-modify-write transaction; persistence is temp file + fsync + os.replace. These tests
use only the public ``add`` / ``remove`` / ``list`` API, so they run unchanged against the old store, where the
lost-update test fails.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import GrantStore

KEY = base64.b64encode(b"c" * 32).decode()

WORKER = textwrap.dedent("""
    import sys, time
    from agentic_os.permissions import GrantStore
    d, who, n, start = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    while time.time() < start:
        time.sleep(0.001)
    s = GrantStore(d)
    made = [s.add("app", f"{who}-{j}", "table", "crm.customers") for j in range(n)]
    for g in made[::2]:
        assert s.remove(g.id) is True
""")


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    for k in ("PERMISSIONS_ALLOW_PLAINTEXT_DEV", "PERMISSIONS_REQUIRE_KEY", "PERMISSIONS_DIR"):
        monkeypatch.delenv(k, raising=False)


def test_a_slow_transaction_does_not_lose_a_concurrent_add(tmp_path):
    """A reads the grants, is slow, and writes; B adds in the meantime. With no lock around the whole
    transaction A writes its stale list over B's grant."""
    a, b = GrantStore(str(tmp_path)), GrantStore(str(tmp_path))
    read_done = threading.Event()
    _R = "_load_raw" if hasattr(GrantStore, "_load_raw") else "_read"
    real_read, first = getattr(GrantStore, _R), threading.local()

    def slow_read(self):
        out = real_read(self)
        if self is a and not getattr(first, "seen", False):
            first.seen = True
            read_done.set()
            time.sleep(0.4)
        return out
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(GrantStore, _R, slow_read)
        t = threading.Thread(target=lambda: a.add("app", "from-a", "table", "crm.customers"))
        t.start()
        assert read_done.wait(5)
        b.add("app", "from-b", "table", "crm.customers")
        t.join(10)
    assert sorted(g.subject for g in GrantStore(str(tmp_path)).list()) == ["from-a", "from-b"]


def test_a_slow_remove_does_not_resurrect_or_drop_other_changes(tmp_path):
    s = GrantStore(str(tmp_path))
    victim = s.add("app", "victim", "table", "crm.customers")
    a, b = GrantStore(str(tmp_path)), GrantStore(str(tmp_path))
    read_done, first = threading.Event(), threading.local()
    _R = "_load_raw" if hasattr(GrantStore, "_load_raw") else "_read"
    real_read = getattr(GrantStore, _R)

    def slow_read(self):
        out = real_read(self)
        if self is a and not getattr(first, "seen", False):
            first.seen = True
            read_done.set()
            time.sleep(0.4)
        return out
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(GrantStore, _R, slow_read)
        t = threading.Thread(target=lambda: a.remove(victim.id))
        t.start()
        assert read_done.wait(5)
        b.add("app", "newcomer", "table", "crm.customers")
        t.join(10)
    assert [g.subject for g in GrantStore(str(tmp_path)).list()] == ["newcomer"]


def test_threads_on_separate_stores_lose_nothing(tmp_path):
    stores = [GrantStore(str(tmp_path)) for _ in range(6)]
    errors: list = []

    def work(i, s):
        try:
            made = [s.add("app", f"t{i}-{j}", "table", "crm.customers") for j in range(15)]
            for g in made[::3]:
                s.remove(g.id)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i, s)) for i, s in enumerate(stores)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    final = GrantStore(str(tmp_path))
    assert errors == [] and not final.sealed
    assert len(final.list()) == 6 * (15 - 5)


def test_processes_lose_nothing_and_the_file_is_never_torn(tmp_path):
    procs, n, who = [], 20, ("p0", "p1", "p2", "p3")
    start = time.time() + 2.0
    env = {**os.environ, "PERMISSIONS_KEY": KEY}
    for w in who:
        procs.append(subprocess.Popen([sys.executable, "-c", WORKER, str(tmp_path), w, str(n), str(start)],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    reader, torn = GrantStore(str(tmp_path)), []
    deadline = time.time() + 120
    while any(p.poll() is None for p in procs) and time.time() < deadline:
        reader.list()
        if reader.sealed:
            torn.append(time.time())
        time.sleep(0.002)
    outs = [(p.wait(30), p.stderr.read().decode()[-400:]) for p in procs]
    assert [rc for rc, _ in outs] == [0] * len(who), outs
    final = GrantStore(str(tmp_path))
    assert torn == [] and not final.sealed
    assert sorted(g.subject for g in final.list()) == sorted(f"{w}-{j}" for w in who for j in range(n) if j % 2)


def test_the_lock_spans_the_whole_transaction_not_just_the_write(tmp_path):
    s = GrantStore(str(tmp_path))
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with s._txn():
            held.set()
            release.wait(10)

    def writer():
        GrantStore(str(tmp_path)).add("app", "late", "table", "crm.customers")
        done.set()
    h = threading.Thread(target=holder)
    h.start()
    held.wait(5)
    w = threading.Thread(target=writer)
    w.start()
    time.sleep(0.3)
    assert not done.is_set()          # blocked while someone else is inside a transaction
    release.set()
    w.join(10)
    h.join(10)
    assert done.is_set()


def test_a_writer_leaves_no_temp_files_behind(tmp_path):
    s = GrantStore(str(tmp_path))
    for i in range(5):
        s.add("app", f"a{i}", "table", "crm.customers")
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")) == []


def test_readers_see_a_complete_file_while_a_writer_replaces_it(tmp_path):
    s = GrantStore(str(tmp_path))
    stop, seen_sealed = threading.Event(), []

    def writer():
        for i in range(60):
            s.add("app", f"w{i}", "table", "crm.customers")
        stop.set()
    t = threading.Thread(target=writer)
    t.start()
    r = GrantStore(str(tmp_path))
    while not stop.is_set():
        r.list()
        seen_sealed.append(r.sealed)
    t.join(30)
    assert not any(seen_sealed)
