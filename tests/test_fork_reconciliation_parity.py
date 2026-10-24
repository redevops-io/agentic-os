"""Fork branches obey the same reconciliation contract as plan nodes.

A plan node whose call may have been sent but whose outcome is unknown parks the mission (WAITING_RECONCILIATION) and is never sent
again; only evidence resolves it, and the SAME mission continues. Fork branches used to behave differently: an ambiguous error became a
terminal BranchFailed (and a join failure), and a branch caught in flight by a restart was sent again, without a reservation check. These
tests pin one execution model for both.

The headline test is the crash-and-restart one the review asked for: three branches, one verified successful, one UNKNOWN, one still
running when the process dies. After the restart the successful branch does not run again, the UNKNOWN one is not sent again, and the third
finishes."""
from __future__ import annotations

import threading
import time

import pytest

from agentic_os.mission import fork_join
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, SqliteEnvelopeStore, SqliteLedger)
from agentic_os.mission.executor import Executor
from agentic_os.mission.fork_join import (
    BranchSpec, ForkSpec, JoinSpec, SimulatedCrash, branch_idempotency_key, branch_node_id, fold_fork)
from agentic_os.mission.policy import STANDING, Effect, MissionPolicy, PolicyRule
from agentic_os.mission.reconcile import ReconciliationConflict, ReconciliationError
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import Budget, MissionState
from agentic_os.mission.waitstate import Wait, classify
from tests._fork_support import CountingOperator, CrashingStore, GRANTS, build_registry

NOW = time.time()
EVIDENCE = {"kind": "provider_record", "reference": "provider dashboard, charge ch_b, 2026-10-24 09:14"}
FID = "f1"


class Provider(CountingOperator):
    """The EXTERNAL system. Executes each idempotency key at most once. `timeout_keys` are calls whose request is written (the effect
    applies when `applied_on_timeout`) and then time out: the caller cannot tell whether it happened."""

    def __init__(self):
        super().__init__()
        self.timeout_keys: set = set()
        self.applied_on_timeout = True

    def invoke(self, operator, capability, inputs, idempotency_key, *, secrets=None):
        if capability == "w.write" and idempotency_key in self.timeout_keys:
            with self.lock:
                self.calls[idempotency_key] = self.calls.get(idempotency_key, 0) + 1
                if self.applied_on_timeout:
                    self.effects.setdefault(idempotency_key, 1)
            raise TimeoutError("timed out after the request was written")
        return super().invoke(operator, capability, inputs, idempotency_key, secrets=secrets)

    def sent(self, key: str) -> int:
        return self.calls.get(key, 0)


class World:
    """One durable deployment: the event log, the authority ledger and the external provider survive every 'restart'."""

    def __init__(self, tmp, monkeypatch):
        monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
        monkeypatch.delenv("AGENTIC_OS_APPROVAL_SECRET", raising=False)
        self.log, self.db, self.op = str(tmp / "events.jsonl"), str(tmp / "auth.db"), Provider()
        SqliteEnvelopeStore(self.db).put(AuthorityEnvelope(
            tenant="acme", principal="owner", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 100_000,
            grants=(Grant("w.*"),), budget=AutonomyBudget(usd=1000, max_actions=1000)))
        self.crashing: "CrashingStore | None" = None

    def boot(self, *, crashing=False) -> MissionRuntime:
        store = CrashingStore(self.log) if crashing else EventStore(self.log)
        if crashing:
            self.crashing = store
        rt = MissionRuntime(build_registry(), Executor(self.op), store=store, max_concurrency=4,
                            authority=AuthorityGuard(SqliteEnvelopeStore(self.db), SqliteLedger(self.db + ".usage")),
                            authority_key=lambda m: "env")
        rt.forks.poll_s = 0.002
        return rt

    def mission(self, rt):
        pol = MissionPolicy(id="p", version="1", rules=(
            PolicyRule("w", Effect.REQUIRE_APPROVAL, "side_effecting", approval_mode=STANDING),))
        return rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding",
                                 budget=Budget(usd=100.0), tenant="acme", principal="owner", policy=pol)

    def restart(self, mid) -> MissionRuntime:
        fork_join._LIVE_OWNERS.clear()            # the old process is gone: its driver claims and its in-process lock are not
        fork_join._DRIVER_LOCKS.clear()           # visible to the new one (the old driver thread here is only a zombie)
        rt = self.boot()
        rt.rehydrate(mid, policy_refs=GRANTS)
        return rt


def three(max_b=1, mode="all") -> ForkSpec:
    """A: a side-effecting write that succeeds. B: a side-effecting write that goes UNKNOWN. C: a read-only call that is slow."""
    return ForkSpec(FID, [BranchSpec("A", "w.write", inputs={"tag": "a"}),
                          BranchSpec("B", "w.write", inputs={"tag": "b"}, max_attempts=max_b),
                          BranchSpec("C", "w.read", inputs={"tag": "c"})],
                    JoinSpec(mode, None, None, "fail", True), max_concurrent=3)


def key(mid, bid, attempt=1) -> str:
    return branch_idempotency_key(mid, FID, bid, attempt)


def lease(g, mid, bid, attempt=1):
    row = g.lease(f"{mid}:{branch_node_id(mid, FID, bid)}:a{attempt}")
    return row["state"] if row else None


def in_thread(fn):
    out: dict = {}

    def run():
        try:
            out["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - a simulated crash is a BaseException
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the scenario to reach its state")


def status(rt, mid) -> dict:
    return {b.branch_id: b.status for b in fold_fork(rt.store.for_mission(mid), FID).ordered()}


def crash_with_three_branches(w):
    """Process 1: A succeeded, B UNKNOWN (sent, timed out), C still running (blocked in the provider). Then the process dies."""
    rt1 = w.boot(crashing=True)
    m = w.mission(rt1)
    w.op.timeout_keys = {key(m.id, "B")}
    w.op.gates["c"] = threading.Event()
    t, out = in_thread(lambda: rt1.forks.run_fork(m.id, three()))
    wait_for(lambda: status(rt1, m.id) == {"A": "succeeded", "B": "reconciling", "C": "running"} and "c" in w.op.order)
    w.crashing.dead = True                          # kill -9: this process writes nothing more
    return rt1, m, t, out


# ───────────────────────── the headline test ─────────────────────────
def test_crash_with_one_succeeded_one_unknown_and_one_running_branch(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt1, m, t1, out1 = crash_with_three_branches(w)
    g = rt1.authority
    ka, kb, kc = key(m.id, "A"), key(m.id, "B"), key(m.id, "C")
    assert (w.op.sent(ka), w.op.sent(kb), w.op.sent(kc)) == (1, 1, 1)
    assert [lease(g, m.id, b) for b in "ABC"] == ["settled", "unknown", "dispatched"]

    rt2 = w.restart(m.id)
    spec = three()
    t2, out2 = in_thread(lambda: rt2.forks.run_fork(m.id, spec))
    wait_for(lambda: w.op.sent(kc) >= 2)            # the read-only branch was picked up and re-sent under its same key
    w.op.gates["c"].set()                           # let the provider answer
    t2.join(10)
    t1.join(10)
    res = out2["result"]

    # the verified successful branch did not run again; the UNKNOWN branch was not sent again; the third one finished
    assert w.op.sent(ka) == 1 and w.op.effects[ka] == 1
    assert w.op.sent(kb) == 1 and w.op.effects[kb] == 1
    assert status(rt2, m.id) == {"A": "succeeded", "B": "reconciling", "C": "succeeded"}
    assert w.op.effects[kc] == 1
    # ... and the fork is waiting for evidence, not failed
    assert res.status == "awaiting_reconciliation"
    assert rt2._missions[m.id].state == MissionState.WAITING_RECONCILIATION
    assert [e.type for e in rt2.store.for_mission(m.id) if e.type in ("JoinFailed", "JoinSatisfied")] == []
    assert not any(e.type == "BranchFailed" for e in rt2.store.for_mission(m.id))
    pend = rt2.repo.pending_reconciliation(m.id)
    assert [p["node_id"] for p in pend] == [branch_node_id(m.id, FID, "B")]
    assert [lease(rt2.authority, m.id, b) for b in "ABC"] == ["settled", "unknown", "settled"]


@pytest.mark.parametrize("again", [0, 1, 2])
def test_further_restarts_while_unknown_never_send_it(tmp_path, monkeypatch, again):
    w = World(tmp_path, monkeypatch)
    rt1, m, t1, out1 = crash_with_three_branches(w)
    kb = key(m.id, "B")
    rt = w.restart(m.id)
    w.op.gates["c"].set()
    assert rt.forks.run_fork(m.id, three()).status == "awaiting_reconciliation"
    for _ in range(again):
        rt = w.restart(m.id)
        assert rt.forks.run_fork(m.id, three()).status == "awaiting_reconciliation"
    assert w.op.sent(kb) == 1


def test_evidence_that_it_executed_completes_the_branch_and_the_same_fork_finishes(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt1, m, t1, out1 = crash_with_three_branches(w)
    rt2 = w.restart(m.id)
    rt2.forks.register_spec(m.id, three())
    w.op.gates["c"].set()
    assert rt2.forks.run_fork(m.id, three()).status == "awaiting_reconciliation"
    nb = branch_node_id(m.id, FID, "B")
    ka, kb = key(m.id, "A"), key(m.id, "B")
    rt2.reconcile_node(m.id, nb, executed=True, evidence=EVIDENCE, actor="ops@acme", reason="charge ch_b is in the dashboard")
    st = fold_fork(rt2.store.for_mission(m.id), FID)
    assert st.closed == "satisfied" and [b.status for b in st.ordered()] == ["succeeded"] * 3
    assert st.branches["B"].result["reconciled"] is True and st.branches["B"].result["reconciled_by"] == "ops@acme"
    assert rt2.repo.pending_reconciliation(m.id) == []
    assert lease(rt2.authority, m.id, "B") == "settled"
    assert w.op.sent(ka) == 1 and w.op.sent(kb) == 1                     # nothing was sent for any of this
    assert rt2._missions[m.id].state != MissionState.WAITING_RECONCILIATION


def test_authoritative_negative_retries_the_branch_as_a_new_attempt_under_a_fresh_reservation(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.op.applied_on_timeout = False                                       # the request never took effect
    rt1 = w.boot()
    m = w.mission(rt1)
    w.op.timeout_keys = {key(m.id, "B")}
    spec = three(max_b=2)
    assert rt1.forks.run_fork(m.id, spec).status == "awaiting_reconciliation"
    nb = branch_node_id(m.id, FID, "B")
    k1, k2 = key(m.id, "B", 1), key(m.id, "B", 2)
    assert w.op.sent(k1) == 1 and w.op.sent(k2) == 0
    rt1.reconcile_node(m.id, nb, executed=False, evidence=EVIDENCE, actor="ops@acme", reason="provider shows no charge for this key")
    st = fold_fork(rt1.store.for_mission(m.id), FID)
    assert st.closed == "satisfied" and st.branches["B"].attempt == 2
    assert w.op.sent(k1) == 1 and w.op.sent(k2) == 1 and w.op.effects.get(k1) is None and w.op.effects[k2] == 1
    assert (lease(rt1.authority, m.id, "B", 1), lease(rt1.authority, m.id, "B", 2)) == ("released", "settled")


def test_authoritative_negative_with_no_attempts_left_fails_the_branch_cleanly(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.op.applied_on_timeout = False
    rt = w.boot()
    m = w.mission(rt)
    w.op.timeout_keys = {key(m.id, "B")}
    assert rt.forks.run_fork(m.id, three(max_b=1)).status == "awaiting_reconciliation"
    rt.reconcile_node(m.id, branch_node_id(m.id, FID, "B"), executed=False, evidence=EVIDENCE, actor="ops@acme",
                      reason="provider shows no charge for this key")
    st = fold_fork(rt.store.for_mission(m.id), FID)
    assert st.closed == "failed" and st.branches["B"].status == "failed" and st.branches["B"].reason == "reconciled_not_executed"
    assert w.op.sent(key(m.id, "B")) == 1                                 # not retried: that was the branch's whole budget
    # it provably did not execute, so it is NOT reported as an unknown effect
    assert not any(r.get("reversibility") == "UNKNOWN_EFFECT" for r in (rt._missions[m.id].outcome or {}).get("not_reversible", []))


# ───────────────────────── live ambiguity ─────────────────────────
def test_an_ambiguous_error_parks_the_branch_instead_of_failing_it(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    w.op.timeout_keys = {key(m.id, "B")}
    res = rt.forks.run_fork(m.id, three())
    assert res.status == "awaiting_reconciliation"
    assert rt._missions[m.id].state == MissionState.WAITING_RECONCILIATION
    assert status(rt, m.id) == {"A": "succeeded", "B": "reconciling", "C": "succeeded"}
    evs = rt.store.for_mission(m.id)
    assert not any(e.type in ("BranchFailed", "JoinFailed") for e in evs)
    req = [e.payload for e in evs if e.type == "ReconciliationRequired"]
    assert len(req) == 1 and req[0]["fork_id"] == FID and req[0]["branch_id"] == "B"
    assert req[0]["lease_id"].endswith(":a1") and req[0]["idempotency_key"] == key(m.id, "B")
    assert lease(rt.authority, m.id, "B") == "unknown"
    # the same views a plan node appears in
    assert [i["node_id"] for i in rt.inbox() if i["kind"] == "reconciliation"] == [branch_node_id(m.id, FID, "B")]
    assert [r for r in rt.missions() if r["id"] == m.id][0]["awaiting_reconciliation"] is True


def test_a_failure_that_says_nothing_was_applied_is_still_retried(tmp_path, monkeypatch):
    """Negative control: parking is for AMBIGUOUS failures only."""
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    seen = {"n": 0}
    real = w.op.invoke

    def flaky(operator, capability, inputs, idempotency_key, *, secrets=None):
        if capability == "w.write" and inputs.get("tag") == "b" and seen["n"] == 0:
            seen["n"] += 1
            e = RuntimeError("rejected before sending")
            e.not_applied = True
            raise e
        return real(operator, capability, inputs, idempotency_key, secrets=secrets)
    w.op.invoke = flaky
    res = rt.forks.run_fork(m.id, three(max_b=2))
    assert res.status == "satisfied"
    assert not any(e.type == "ReconciliationRequired" for e in rt.store.for_mission(m.id))
    assert w.op.sent(key(m.id, "B", 2)) == 1


def test_a_failed_read_only_branch_is_not_a_reconciliation(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    w.op.fail_caps = {"w.read"}
    res = rt.forks.run_fork(m.id, three())
    assert res.status == "failed"
    assert not any(e.type == "ReconciliationRequired" for e in rt.store.for_mission(m.id))


def test_a_failed_join_waits_for_the_unknown_branch_instead_of_compensating_around_it(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    w.op.fail_caps = {"w.read"}                                           # C fails: an 'all' join can no longer succeed
    w.op.timeout_keys = {key(m.id, "B")}                                  # B is unknown: whether it applied decides what to roll back
    res = rt.forks.run_fork(m.id, three())
    assert res.status == "awaiting_reconciliation"
    assert not any(e.type == "JoinFailed" for e in rt.store.for_mission(m.id))
    assert rt._missions[m.id].state == MissionState.WAITING_RECONCILIATION
    rt.forks.register_spec(m.id, three())
    rt.reconcile_node(m.id, branch_node_id(m.id, FID, "B"), executed=True, evidence=EVIDENCE, actor="ops@acme",
                      reason="charge ch_b is in the dashboard")
    st = fold_fork(rt.store.for_mission(m.id), FID)
    assert st.closed == "failed" and st.branches["B"].status == "succeeded"
    assert w.op.sent(key(m.id, "B")) == 1


def test_cancelling_leaves_the_unknown_effect_reported_and_the_reconciliation_open(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    w.op.timeout_keys = {key(m.id, "B")}
    assert rt.forks.run_fork(m.id, three()).status == "awaiting_reconciliation"
    rt.forks.cancel_branch(m.id, FID, "B", "operator gave up")
    st = fold_fork(rt.store.for_mission(m.id), FID)
    assert st.branches["B"].status == "cancelled" and st.branches["B"].awaiting       # cancelling does not clear an uncertainty
    assert [p["node_id"] for p in rt.repo.pending_reconciliation(m.id)] == [branch_node_id(m.id, FID, "B")]
    assert lease(rt.authority, m.id, "B") == "unknown"
    # a later confirmation is kept as a LATE receipt: it never changes a branch the fork gave up on
    rt.reconcile_node(m.id, branch_node_id(m.id, FID, "B"), executed=True, evidence=EVIDENCE, actor="ops@acme",
                      reason="charge ch_b is in the dashboard", result={"charge": "ch_b"})
    st = fold_fork(rt.store.for_mission(m.id), FID)
    assert st.branches["B"].status == "cancelled" and len(st.late_results) == 1 and rt.repo.pending_reconciliation(m.id) == []


# ───────────────────────── restart: what the reservation says ─────────────────────────
def test_a_side_effecting_branch_in_flight_at_the_crash_is_not_sent_again(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt1 = w.boot(crashing=True)
    m = w.mission(rt1)
    w.op.gates["b"] = threading.Event()                                  # B's write is in the provider when the process dies
    t1, _ = in_thread(lambda: rt1.forks.run_fork(m.id, three()))
    wait_for(lambda: status(rt1, m.id) == {"A": "succeeded", "B": "running", "C": "succeeded"} and "b" in w.op.order)
    w.crashing.dead = True
    kb = key(m.id, "B")
    assert w.op.sent(kb) == 1
    rt2 = w.restart(m.id)
    res = rt2.forks.run_fork(m.id, three())
    assert res.status == "awaiting_reconciliation"
    assert w.op.sent(kb) == 1                                            # the unknown call was NOT repeated
    assert status(rt2, m.id)["B"] == "reconciling" and lease(rt2.authority, m.id, "B") == "unknown"
    w.op.gates["b"].set()                                                # the orphaned call completes at the provider, once
    wait_for(lambda: w.op.effects.get(kb) == 1)
    assert w.op.sent(kb) == 1


def _b_in_flight_then_crash(w, max_b=1):
    rt1 = w.boot(crashing=True)
    m = w.mission(rt1)
    w.op.gates["b"] = threading.Event()
    t1, _ = in_thread(lambda: rt1.forks.run_fork(m.id, three(max_b=max_b)))
    # A and C must have committed before the process dies: on a slow machine they can still be in flight, and then they (correctly) wait for
    # reconciliation after the restart, which is a different scenario from the one this helper sets up
    wait_for(lambda: status(rt1, m.id) == {"A": "succeeded", "B": "running", "C": "succeeded"} and "b" in w.op.order)
    w.crashing.dead = True
    return rt1, m, t1


def _b_lease(rt, m):
    return f"{m.id}:{branch_node_id(m.id, FID, 'B')}:a1"


def test_a_reservation_settled_elsewhere_while_the_driver_was_down_waits_for_the_result_and_is_not_resent(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt1, m, t1 = _b_in_flight_then_crash(w)
    rt1.authority.reconcile(_b_lease(rt1, m), executed=True)             # an operator settled it in the ledger directly
    rt2 = w.restart(m.id)
    assert rt2.forks.run_fork(m.id, three()).status == "awaiting_reconciliation"
    assert w.op.sent(key(m.id, "B")) == 1 and status(rt2, m.id)["B"] == "reconciling"     # confirmed, but no result to publish yet
    w.op.gates["b"].set()


@pytest.mark.parametrize("max_b,final", [(2, "satisfied"), (1, "failed")])
def test_a_reservation_released_elsewhere_while_the_driver_was_down_is_a_clean_retry_or_a_clean_failure(tmp_path, monkeypatch, max_b, final):
    w = World(tmp_path, monkeypatch)
    rt1, m, t1 = _b_in_flight_then_crash(w, max_b)
    rt1.authority.reconcile(_b_lease(rt1, m), executed=False)            # provably did not execute
    w.op.gates["b"].set()
    rt2 = w.restart(m.id)
    res = rt2.forks.run_fork(m.id, three(max_b=max_b))
    assert res.status == final
    if final == "satisfied":
        assert w.op.sent(key(m.id, "B", 2)) == 1 and w.op.effects[key(m.id, "B", 2)] == 1
    else:
        assert w.op.sent(key(m.id, "B", 2)) == 0
    assert not any(e.type == "ReconciliationRequired" for e in rt2.store.for_mission(m.id))


def test_the_dispatch_backstop_parks_a_branch_whose_reservation_was_already_dispatched(tmp_path, monkeypatch):
    """Defence in depth: even if the resume check is bypassed, the reservation's single dispatch refuses a second send."""
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    real = rt._lease_dispatched

    def dispatch_then_die(mm, node):
        real(mm, node)
        raise SimulatedCrash("after the reservation was dispatched, before the call")
    rt._lease_dispatched = dispatch_then_die
    one = lambda: ForkSpec(FID, [BranchSpec("A", "w.write", inputs={"tag": "a"})], JoinSpec("all", None, None, "fail", True))  # noqa: E731
    with pytest.raises(SimulatedCrash):
        rt.forks.run_fork(m.id, one())
    rt._lease_dispatched = real
    fork_join._LIVE_OWNERS.clear()
    rt.forks._resume_lease = lambda *a, **k: (False, False)               # bypass the resume check
    assert rt.forks.run_fork(m.id, one()).status == "awaiting_reconciliation"
    assert w.op.sent(key(m.id, "A")) == 0                                # never sent: dispatched means 'maybe', so it waits for evidence
    assert [p["node_id"] for p in rt.repo.pending_reconciliation(m.id)] == [branch_node_id(m.id, FID, "A")]


def test_a_reserved_but_never_dispatched_branch_is_sent_exactly_once_after_the_restart(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt1 = w.boot()
    m = w.mission(rt1)

    def die(*a, **k):
        raise SimulatedCrash("before dispatch")
    rt1._lease_dispatched = die                                          # crash after BranchStarted, before the call leaves
    with pytest.raises(SimulatedCrash):
        rt1.forks.run_fork(m.id, ForkSpec(FID, [BranchSpec("A", "w.write", inputs={"tag": "a"})], JoinSpec("all", None, None, "fail", True)))
    ka = key(m.id, "A")
    assert w.op.sent(ka) == 0 and lease(rt1.authority, m.id, "A") == "reserved"
    rt2 = w.restart(m.id)
    res = rt2.forks.run_fork(m.id, ForkSpec(FID, [BranchSpec("A", "w.write", inputs={"tag": "a"})], JoinSpec("all", None, None, "fail", True)))
    assert res.status == "satisfied" and w.op.sent(ka) == 1 and w.op.effects[ka] == 1
    assert lease(rt2.authority, m.id, "A") == "settled"


def test_a_success_recorded_before_the_ledger_was_settled_is_settled_on_restart_and_not_rerun(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt1 = w.boot()
    m = w.mission(rt1)
    one = lambda: ForkSpec(FID, [BranchSpec("A", "w.write", inputs={"tag": "a"}), BranchSpec("D", "w.write", inputs={"tag": "d"})],  # noqa: E731
                           JoinSpec("all", None, None, "fail", True), max_concurrent=1)

    def die(*a, **k):
        raise SimulatedCrash("after BranchSucceeded, before the ledger")
    rt1._lease_settle = die
    with pytest.raises(SimulatedCrash):
        rt1.forks.run_fork(m.id, one())
    ka = key(m.id, "A")
    assert status(rt1, m.id)["A"] == "succeeded" and lease(rt1.authority, m.id, "A") == "dispatched"
    rt2 = w.restart(m.id)
    assert rt2.forks.run_fork(m.id, one()).status == "satisfied"
    assert lease(rt2.authority, m.id, "A") == "settled"                  # the log is the proof; the counters are not held for a finished call
    assert w.op.sent(ka) == 1 and w.op.effects[ka] == 1


# ───────────────────────── resolution safety ─────────────────────────
def test_a_second_resolution_the_other_way_is_a_conflict_and_the_same_one_is_idempotent(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    w.op.timeout_keys = {key(m.id, "B")}
    rt.forks.run_fork(m.id, three())
    nb = branch_node_id(m.id, FID, "B")
    rt.reconcile_node(m.id, nb, executed=True, evidence=EVIDENCE, actor="ops@acme", reason="charge ch_b is in the dashboard",
                      result={"charge": "ch_b"})
    n = len(rt.store.for_mission(m.id))
    with pytest.raises(ReconciliationConflict):
        rt.reconcile_node(m.id, nb, executed=False, evidence=EVIDENCE, actor="ops2@acme", reason="no, it is not in the dashboard")
    rt.reconcile_node(m.id, nb, executed=True, evidence=EVIDENCE, actor="ops@acme", reason="charge ch_b is in the dashboard",
                      result={"charge": "ch_b"})
    assert len(rt.store.for_mission(m.id)) == n                          # repeating the decision changes nothing
    assert w.op.sent(key(m.id, "B")) == 1


def test_reconciling_a_branch_requires_the_same_evidence_as_a_plan_node(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    w.op.timeout_keys = {key(m.id, "B")}
    rt.forks.run_fork(m.id, three())
    nb = branch_node_id(m.id, FID, "B")
    for kw in (dict(evidence={}, actor="a", reason="a good reason here"),
               dict(evidence=EVIDENCE, actor=" ", reason="a good reason here"),
               dict(evidence=EVIDENCE, actor="a", reason="x")):
        with pytest.raises(ReconciliationError):
            rt.reconcile_node(m.id, nb, executed=True, **kw)
    assert [p["node_id"] for p in rt.repo.pending_reconciliation(m.id)] == [nb]          # nothing changed
    assert lease(rt.authority, m.id, "B") == "unknown"


def test_a_branch_that_produces_a_fact_needs_the_real_result(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    s = lambda: ForkSpec(FID, [BranchSpec("B", "w.write", inputs={"tag": "b"}, produces="out1")], JoinSpec("all", None, None, "fail", True))  # noqa: E731
    w.op.timeout_keys = {key(m.id, "B")}
    assert rt.forks.run_fork(m.id, s()).status == "awaiting_reconciliation"
    nb = branch_node_id(m.id, FID, "B")
    with pytest.raises(ReconciliationError, match="real result"):
        rt.reconcile_node(m.id, nb, executed=True, evidence=EVIDENCE, actor="ops@acme", reason="charge ch_b is in the dashboard")
    assert lease(rt.authority, m.id, "B") == "unknown"


# ───────────────────────── one model for the watchdog and the supervisor ─────────────────────────
def test_a_waiting_fork_is_not_a_stalled_mission_and_the_supervisor_can_see_it(tmp_path, monkeypatch):
    from agentic_os.mission.probes import ProbeOutcome as O, ProbeRegistry, ProbeResult, Qualification
    from agentic_os.mission.reconcile_jobs import ReconcileJobs, ReconciliationSupervisor, RetryPolicy
    w = World(tmp_path, monkeypatch)
    rt = w.boot()
    m = w.mission(rt)
    w.op.timeout_keys = {key(m.id, "B")}
    rt.forks.run_fork(m.id, three())
    rt.forks.register_spec(m.id, three())
    a = classify(rt, m.id, now=time.time() + 100_000)                    # however long it has been quiet
    assert a.kind in (Wait.WAITING_OBSERVATION, Wait.WAITING_ESCALATED) and a.watchdog_eligible is False
    reg = ProbeRegistry()
    reg.register("w.*", lambda ctx: ProbeResult(O.CONFIRMED_EXECUTED, evidence=dict(EVIDENCE), provider_ref="ch_b",
                                                result={"charge": "ch_b"}, observed_at=ctx.now),
                 Qualification("provider", eventually_consistent=False, authoritative_negative=True))
    now = [time.time()]
    sup = ReconciliationSupervisor(rt, ReconcileJobs(str(tmp_path / "jobs.db")), reg,
                                   policy=RetryPolicy(max_probes=3, initial_s=1, max_s=5, factor=2.0, jitter=0.0), clock=lambda: now[0])
    assert sup.scan() == 1
    now[0] += 60
    sup.tick()
    st = fold_fork(rt.store.for_mission(m.id), FID)
    assert st.closed == "satisfied" and st.branches["B"].result["reconciled"] is True
    assert w.op.sent(key(m.id, "B")) == 1                                # probing is read-only: the business action was never repeated
