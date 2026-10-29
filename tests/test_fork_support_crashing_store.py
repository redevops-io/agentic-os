"""Regression tests for the fault-injection store the fork/join crash tests rely on (tests/_fork_support.py).

`CrashingStore` must make 'the Nth append' well defined when the driver and branch threads append at once. It used to count,
write and only then compare the counter, unlocked: two overlapping appends could both increment before either compared, so
neither saw N, the crash never fired and the run finished with `appended > N` (CI: `assert (False or 13 <= 12)`)."""
from __future__ import annotations

import threading
import time

import pytest

from agentic_os.mission.fork_join import SimulatedCrash
from agentic_os.mission.store import EventStore
from tests._fork_support import CrashingStore


def _durable_types(path):
    return [e.type for e in EventStore(path).all()]


# 1. the race, made deterministic ---------------------------------------------------------------------------------------
def test_two_racing_appends_cannot_both_skip_the_armed_threshold(tmp_path, monkeypatch):
    """t1 makes the targeted (2nd) append and is held INSIDE its durable write until t2 has either incremented the counter
    (the old, unlocked store: both then miss the threshold) or been shut out (the atomic store). No sleep is used to order
    the two threads; the bounded wait only gives t2 a chance to prove it can enter, and returns at once when it does."""
    path = str(tmp_path / "race.jsonl")
    store = CrashingStore(path, "after")
    store.arm(2)
    store.append("pre", "m", {})                                 # append 1: below the threshold
    t1_in_write, results = threading.Event(), {}
    real = EventStore.append

    def held_write(self, *a, **k):
        ev = real(self, *a, **k)                                 # the event is durably written ...
        if threading.current_thread().name == "t1":
            t1_in_write.set()                                    # ... and t1 stays inside the store ...
            deadline = time.monotonic() + 0.3
            while self.appended < 3 and time.monotonic() < deadline:
                time.sleep(0.001)                                # ... until t2 shows it got past the counter, or it cannot
        return ev
    monkeypatch.setattr(EventStore, "append", held_write)

    def run(tag):
        try:
            store.append(f"T{tag}", "m", {})
            results[tag] = "returned"
        except SimulatedCrash as e:
            results[tag] = str(e)

    t1 = threading.Thread(target=run, args=("1",), name="t1")
    t2 = threading.Thread(target=run, args=("2",), name="t2")
    t1.start()
    assert t1_in_write.wait(5)
    t2.start()
    t1.join(5)
    t2.join(5)
    assert not t1.is_alive() and not t2.is_alive()
    # exactly one append was the 2nd: t1 crashed AFTER its write; t2 was the 3rd and found the process dead
    assert results == {"1": "after T1", "2": "dead"}, results
    assert store.appended == 2 and store.dead and store.countdown is None, (store.appended, store.dead)
    assert _durable_types(path) == ["pre", "T1"]


# 2. before / after semantics are exactly those of the original store --------------------------------------------------
@pytest.mark.parametrize("mode,persisted_at_crash", [("before", ["e1", "e2"]), ("after", ["e1", "e2", "e3"])])
def test_crash_fires_at_exactly_the_nth_append_with_the_documented_durability(tmp_path, mode, persisted_at_crash):
    path = str(tmp_path / f"{mode}.jsonl")
    store = CrashingStore(path, mode)
    seen = []
    store.subscribe(lambda ev: seen.append(ev.type))
    store.arm(3)
    store.append("e1", "m", {})
    store.append("e2", "m", {})
    assert not store.dead and store.countdown == 3
    with pytest.raises(SimulatedCrash, match=f"^{mode} e3$"):
        store.append("e3", "m", {})
    assert store.appended == 3 and store.dead and store.countdown is None
    assert _durable_types(path) == persisted_at_crash                    # 'before': e3 never reached disk; 'after': it did
    assert [e.type for e in store.all()] == persisted_at_crash           # and the in-memory log agrees
    assert seen == persisted_at_crash                                     # subscribers saw every persisted event, once


def test_unarmed_store_counts_but_never_crashes_and_notifies_every_event(tmp_path):
    store = CrashingStore(str(tmp_path / "plain.jsonl"), "after")
    seen = []
    store.subscribe(lambda ev: seen.append(ev.type))
    for i in range(5):
        store.append(f"e{i}", "m", {})
    assert store.appended == 5 and not store.dead and seen == [f"e{i}" for i in range(5)]
    store.arm(10 ** 9)                                                    # arm() restarts the count, as the tests rely on
    assert store.appended == 0
    store.append("x", "m", {})
    assert store.appended == 1 and not store.dead


# 3. after a crash nothing more is written, on any thread ---------------------------------------------------------------
@pytest.mark.parametrize("mode", ["before", "after"])
def test_after_a_crash_no_append_succeeds_on_any_thread_and_nothing_is_written(tmp_path, mode):
    path = str(tmp_path / f"dead_{mode}.jsonl")
    store = CrashingStore(path, mode)
    store.arm(1)
    with pytest.raises(SimulatedCrash):
        store.append("boom", "m", {})
    durable, count = _durable_types(path), store.appended
    out = []

    def late(i):
        try:
            store.append(f"late{i}", "m", {})
            out.append("returned")
        except SimulatedCrash as e:
            out.append(str(e))
    ts = [threading.Thread(target=late, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join(5) for t in ts]
    assert out == ["dead"] * 6                                            # every orphan sees the dead process
    assert store.appended == count and _durable_types(path) == durable    # not counted, not written


def test_dead_can_be_set_from_outside_to_model_a_hard_kill(tmp_path):
    path = str(tmp_path / "kill9.jsonl")
    store = CrashingStore(path)
    store.append("ok", "m", {})
    store.dead = True
    with pytest.raises(SimulatedCrash, match="^dead$"):
        store.append("never", "m", {})
    assert _durable_types(path) == ["ok"]


# 4. the gate must never be held across a subscriber (lock-order inversion with the fork/join controller) ---------------
def test_gate_is_not_held_while_subscribers_run(tmp_path):
    """The controller's subscriber takes the controller lock, and the controller appends while holding it. If the store held
    its gate across subscribers, thread A (gate -> subscriber -> controller lock) and thread B (controller lock -> gate)
    would deadlock. Modelled with an external lock."""
    store = CrashingStore(str(tmp_path / "order.jsonl"))
    ctl = threading.RLock()                                               # stands in for ForkJoinController._lock
    b_holds_ctl = threading.Event()

    def subscriber(ev):
        if ev.type == "A":
            assert b_holds_ctl.wait(5)
            with ctl:                                                     # needs B to finish its append first
                pass
    store.subscribe(subscriber)

    def thread_b():
        with ctl:
            b_holds_ctl.set()
            store.append("B", "m", {})                                    # needs the gate while A is in its subscriber
    tb = threading.Thread(target=thread_b, daemon=True)          # daemon: a regression must fail the test, not hang the run
    ta = threading.Thread(target=lambda: store.append("A", "m", {}), daemon=True)
    ta.start()
    tb.start()
    ta.join(10)
    tb.join(10)
    assert not ta.is_alive() and not tb.is_alive(), "deadlock: the store's gate was held across a subscriber"
    assert sorted(e.type for e in store.all()) == ["A", "B"]
