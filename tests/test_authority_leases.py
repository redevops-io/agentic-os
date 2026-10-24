"""Reservation lifecycle: RESERVED -> DISPATCHED -> SETTLED, plus RELEASED and UNKNOWN.

A reservation that might already have executed is never released by time; every transition is atomic and idempotent, so
concurrent workers and retries cannot double-count, double-release, or release an action that left the building."""
from __future__ import annotations

import threading

import pytest

from agentic_os.mission.authority import Lease, MemoryLedger, SqliteLedger

LIM = {"n": 3.0}


@pytest.fixture(params=["memory", "sqlite"])
def led(request, tmp_path):
    return MemoryLedger() if request.param == "memory" else SqliteLedger(str(tmp_path / "u.db"))


def reserve(led, lid, n=1.0, **kw):
    return led.reserve_lease(lid, "k", {"n": n}, LIM, **kw)


def used(led):
    return led.used("k").get("n", 0.0)


def test_happy_path_reserved_dispatched_settled(led):
    assert reserve(led, "a") is None and led.lease_state("a") == Lease.RESERVED.value and used(led) == 1.0
    assert led.lease_dispatched("a") and led.lease_state("a") == Lease.DISPATCHED.value
    assert led.lease_settle("a") and led.lease_state("a") == Lease.SETTLED.value and used(led) == 1.0


def test_settle_replaces_the_reserved_amount_with_the_actual_consumption(led):
    reserve(led, "a", n=2.0)
    led.lease_dispatched("a")
    assert led.lease_settle("a", {"n": 0.5}) and used(led) == 0.5
    assert led.lease_settle("a", {"n": 9.0}) and used(led) == 0.5                 # idempotent: second settle changes nothing


def test_reserve_is_idempotent_per_lease(led):
    assert reserve(led, "a") is None and reserve(led, "a") is None and reserve(led, "a") is None
    assert used(led) == 1.0                                                      # a retried gate never counts twice
    led.lease_dispatched("a"); led.lease_settle("a")
    assert reserve(led, "a") is None and used(led) == 1.0                        # nor a replay after settlement


def test_cancel_before_dispatch_releases_and_a_rereserve_is_fresh(led):
    reserve(led, "a")
    assert led.lease_release("a") and used(led) == 0.0 and led.lease_state("a") == Lease.RELEASED.value
    assert led.lease_release("a") and used(led) == 0.0                           # idempotent: never goes negative / double-returns
    assert reserve(led, "a") is None and used(led) == 1.0 and led.lease_state("a") == Lease.RESERVED.value


def test_a_dispatched_reservation_cannot_be_released_by_the_simple_release(led):
    reserve(led, "a")
    led.lease_dispatched("a")
    assert not led.lease_release("a") and used(led) == 1.0                       # it may have executed
    assert led.lease_state("a") == Lease.DISPATCHED.value


def test_unknown_is_retained_until_reconciled_then_settles_or_releases(led):
    for lid, executed in (("x", True), ("y", False)):
        reserve(led, lid)
        led.lease_dispatched(lid)
        assert led.lease_unknown(lid) and led.lease_state(lid) == Lease.UNKNOWN.value
        assert not led.lease_release(lid)
    assert used(led) == 2.0                                                      # both still counted
    assert led.lease_reconcile("x", executed=True) and led.lease_state("x") == Lease.SETTLED.value
    assert led.lease_reconcile("y", executed=False) and led.lease_state("y") == Lease.RELEASED.value
    assert used(led) == 1.0                                                      # confirmed-not-executed returned its share
    assert led.lease_reconcile("y", executed=False)                              # idempotent
    assert not led.lease_reconcile("y", executed=True)                           # a conflicting late claim is refused
    assert not led.lease_reconcile("x", executed=False)


def test_out_of_order_transitions_are_refused(led):
    reserve(led, "a")
    assert not led.lease_settle("a")                                             # cannot settle what was never dispatched
    assert not led.lease_unknown("a")
    assert not led.lease_dispatched("missing") and not led.lease_release("missing")
    led.lease_release("a")
    assert not led.lease_dispatched("a") and not led.lease_settle("a")


def test_sweep_releases_only_undispatched_old_reservations(led):
    reserve(led, "old", now=1000.0)
    reserve(led, "fresh", now=100_000.0)
    reserve(led, "sent", now=1000.0)
    led.lease_dispatched("sent")
    led.lease_unknown("sent")
    assert led.lease_sweep(older_than_s=3600, now=101_000.0) == 1                  # exactly the stale undispatched one
    assert led.lease_state("old") == Lease.RELEASED.value
    assert led.lease_state("fresh") == Lease.RESERVED.value                        # too young
    assert led.lease_state("sent") == Lease.UNKNOWN.value                          # a lease expiry never frees a possible effect
    assert used(led) == 2.0
    assert led.lease_sweep(older_than_s=3600, now=10_000_000.0) == 1               # later: fresh goes, sent still stays
    assert led.lease_state("sent") == Lease.UNKNOWN.value and used(led) == 1.0


def test_outstanding_lists_everything_that_still_needs_a_decision(led):
    reserve(led, "a"); reserve(led, "b"); reserve(led, "c")
    led.lease_dispatched("b"); led.lease_dispatched("c"); led.lease_settle("c")
    led.lease_unknown("b")
    assert led.leases_outstanding("k") == [("a", "reserved"), ("b", "unknown")]
    assert led.leases_outstanding("other") == []


def test_limits_are_enforced_across_leases_and_a_release_frees_capacity(led):
    for i in range(3):
        assert reserve(led, f"l{i}") is None
    assert reserve(led, "l3") == "n"                                             # limit 3 reached
    led.lease_release("l1")
    assert reserve(led, "l3") is None


def test_rate_hit_is_returned_with_a_released_reservation(led):
    r = ("rate", 1, 3600.0)
    assert reserve(led, "a", rate=r, now=1000.0) is None
    assert reserve(led, "b", rate=r, now=1001.0) == "rate"
    led.lease_release("a")
    assert reserve(led, "b", rate=r, now=1002.0) is None                         # the window slot came back


def test_concurrent_workers_cannot_double_release_or_double_settle(led):
    reserve(led, "a")
    results = []

    def go():
        results.append(led.lease_release("a"))

    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(results) and used(led) == 0.0                                     # exactly one return of the capacity
    reserve(led, "b"); led.lease_dispatched("b")
    out = []
    ts = [threading.Thread(target=lambda: out.append(led.lease_settle("b", {"n": 0.25}))) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(out) and used(led) == 0.25


def test_concurrent_reserve_of_one_lease_counts_once(led):
    out = []
    ts = [threading.Thread(target=lambda: out.append(reserve(led, "same"))) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count(None) == 12 and used(led) == 1.0


def test_sqlite_lifecycle_survives_restart(tmp_path):
    p = str(tmp_path / "u.db")
    a = SqliteLedger(p)
    reserve(a, "x"); a.lease_dispatched("x"); a.lease_unknown("x")
    b = SqliteLedger(p)                                                          # "process restart"
    assert b.lease_state("x") == Lease.UNKNOWN.value and b.leases_outstanding("k") == [("x", "unknown")]
    assert b.lease_reconcile("x", executed=False) and b.used("k").get("n", 0.0) == 0.0
