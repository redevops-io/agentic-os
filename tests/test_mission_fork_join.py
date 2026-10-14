"""Mission fork/join (docs/MISSION_FORK_JOIN.md): parallel branches inside an existing Mission."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from agentic_os.mission import approval as appr
from agentic_os.mission.fork_join import (
    BranchSpec, FailureDecision, ForkError, ForkJoinController, ForkLimits, ForkSpec, JoinSpec,
    SimulatedCrash, branch_idempotency_key, branch_node_id, evaluate_join, fold_fork, fold_forks)
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import Budget, MissionState, NodeCost
from tests._fork_support import CountingOperator, CrashingStore, GRANTS, build, spec, types


# ───────────────────────── pure model ─────────────────────────
@pytest.mark.parametrize("mode,k", [("all", None), ("any", None), ("quorum", 1), ("quorum", 2), ("quorum", 3)])
def test_join_verdict_matrix_is_total_and_consistent(mode, k):
    n = 4
    for s, f, c in itertools.product(range(n + 1), repeat=3):
        if s + f + c > n:
            continue
        v = evaluate_join(mode, k, n, s, f, c)
        need = {"all": n, "any": 1, "quorum": k}[mode]
        if s >= need:
            assert v == "satisfied"
        elif mode == "all":
            assert v == ("failed" if f + c else "pending")
        elif s + (n - s - f - c) < need:
            assert v == "failed"
        else:
            assert v == "pending"
        if v == "pending":                      # never decide while the outcome is still reachable
            assert s + (n - s - f - c) >= need


def test_ids_embed_mission_fork_branch_and_keys_are_deterministic():
    a = branch_node_id("m1", "f", "a")
    assert a != branch_node_id("m1", "f", "b") != branch_node_id("m1", "g", "b") != branch_node_id("m2", "f", "b")
    k = branch_idempotency_key("m", "f", "b", 1)
    assert k == branch_idempotency_key("m", "f", "b", 1)
    assert len({branch_idempotency_key(*x) for x in [("m", "f", "b", 1), ("m", "f", "b", 2), ("m", "f", "c", 1),
                                                       ("m", "g", "b", 1), ("n", "f", "b", 1)]}) == 5


def test_approval_token_for_branch_a_cannot_run_branch_b():
    inputs = {"tag": "x"}
    na, nb = branch_node_id("m1", "f", "a"), branch_node_id("m1", "f", "b")
    tok = appr.mint_approval_token("s3cret", operator="w-op", capability="w.gated", mission_id="m1",
                                   node_id=na, inputs=inputs, marker={"approved": True})
    assert appr.verify_approval_token("s3cret", tok, operator="w-op", capability="w.gated", inputs=inputs,
                                      mission_id="m1", node_id=na)
    for bad in (nb, branch_node_id("m1", "g", "a"), branch_node_id("m2", "f", "a"), None, ""):
        assert appr.verify_approval_token("s3cret", tok, operator="w-op", capability="w.gated", inputs=inputs,
                                          mission_id="m1", node_id=bad) is None


# ───────────────────────── limits ─────────────────────────
def test_default_limits():
    lim = ForkLimits()
    assert lim.for_tenant("any") == {"max_concurrent_branches": 4, "max_branches": 16, "max_open_forks": 4}
    lim = ForkLimits(per_tenant={"t1": {"max_branches": 2, "max_concurrent_branches": 1, "bogus": 9}})
    assert lim.for_tenant("t1") == {"max_concurrent_branches": 1, "max_branches": 2, "max_open_forks": 4}
    assert lim.for_tenant("t2")["max_branches"] == 16


def test_limits_env(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_FORK_MAX_CONCURRENT", "2")
    monkeypatch.setenv("AGENTIC_OS_FORK_TENANT_LIMITS", json.dumps({"x": {"max_branches": 3}}))
    lim = ForkLimits.from_env()
    assert lim.for_tenant("")["max_concurrent_branches"] == 2 and lim.for_tenant("x")["max_branches"] == 3


def test_too_many_branches_rejected_before_anything_runs():
    rt, op, m = build(limits=ForkLimits(per_tenant={"t": {"max_branches": 2}}), tenant="t")
    with pytest.raises(ForkError, match="max_branches"):
        rt.forks.run_fork(m.id, spec(n=3))
    assert op.calls == {} and "ForkRejected" in types(rt, m.id) and "ForkOpened" not in types(rt, m.id)
    # a different tenant is unaffected by tenant t's limit
    rt2, op2, m2 = build(limits=ForkLimits(per_tenant={"t": {"max_branches": 2}}), tenant="u")
    assert rt2.forks.run_fork(m2.id, spec(n=3)).status == "satisfied"


@pytest.mark.parametrize("bad", [
    lambda: spec(n=0), lambda: spec(mode="quorum", k=0), lambda: spec(mode="quorum", k=4),
    lambda: spec(mode="nope"), lambda: spec(on_timeout="x"),
    lambda: ForkSpec("f", [BranchSpec("a", "w.write"), BranchSpec("a", "w.write")], JoinSpec()),
    lambda: ForkSpec("bad id!", [BranchSpec("a", "w.write")], JoinSpec()),
    lambda: ForkSpec("f", [BranchSpec("a", "w.write", max_attempts=0)], JoinSpec()),
])
def test_invalid_specs_rejected(bad):
    rt, op, m = build()
    with pytest.raises(ForkError):
        rt.forks.run_fork(m.id, bad())
    assert op.calls == {}


def test_branch_cannot_name_a_capability_the_mission_was_not_granted():
    rt, op, m = build()
    for bs in (BranchSpec("a", "w.nogrant"), BranchSpec("a", "w.write", requires=("w.nogrant",))):
        with pytest.raises(ForkError, match="not granted"):
            rt.forks.run_fork(m.id, ForkSpec("f", [bs], JoinSpec()))
    assert op.calls == {}


def test_max_open_forks():
    rt, op, m = build(limits=ForkLimits(max_open_forks=1))
    op.gates["b0"] = threading.Event()
    t = threading.Thread(target=lambda: rt.forks.run_fork(m.id, spec("f1", n=1)))
    t.start()
    for _ in range(500):
        if "ForkOpened" in types(rt, m.id):
            break
        time.sleep(0.002)
    with pytest.raises(ForkError, match="max_open_forks"):
        rt.forks.run_fork(m.id, spec("f2", n=1))
    op.gates["b0"].set()
    t.join(5)


def test_effective_concurrency_is_tenant_capped_and_default_is_four():
    rt, op, m = build(op=CountingOperator(delay=0.03))
    rt.forks.run_fork(m.id, spec(n=8))
    opened = next(e for e in rt.store.for_mission(m.id) if e.type == "ForkOpened").payload
    assert opened["limits"]["effective_concurrency"] == 4 and op.peak <= 4
    rt, op, m = build(op=CountingOperator(delay=0.03), tenant="t1",
                      limits=ForkLimits(per_tenant={"t1": {"max_concurrent_branches": 2}}))
    rt.forks.run_fork(m.id, spec(n=8, max_concurrent=8))
    assert op.peak <= 2


def test_tenant_slot_pool_is_shared_across_missions_and_isolated_between_tenants():
    store = EventStore()
    lim = ForkLimits(per_tenant={"A": {"max_concurrent_branches": 2}, "B": {"max_concurrent_branches": 4}})
    opA, opB = CountingOperator(delay=0.05), CountingOperator(delay=0.05)
    rtA, _, m1 = build(store=store, op=opA, tenant="A", limits=lim)
    m2 = rtA.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", tenant="A")
    ts = [threading.Thread(target=_guard(lambda mid=mid, f=f: rtA.forks.run_fork(mid, spec(f, n=4))))
          for mid, f in ((m1.id, "fa"), (m2.id, "fb"))]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert not ERRORS, ERRORS
    assert opA.peak <= 2 and len(opA.effects) == 8       # two missions of tenant A share 2 slots
    rtB, _, m3 = build(op=opB, tenant="B", limits=lim)
    rtB.forks.run_fork(m3.id, spec(n=4))
    assert opB.peak > 2                                  # tenant B not throttled by A's tighter limit


# ───────────────────────── scheduling determinism ─────────────────────────
_DET = r"""
import json, sys
from tests._fork_support import build, spec
from agentic_os.mission.fork_join import fold_fork
conc = int(sys.argv[1])
rt, op, m = build(concurrency=conc)
rt.forks.run_fork("MID", spec(n=6, max_concurrent=conc)) if False else None
rt.forks.run_fork(m.id, spec(n=6, max_concurrent=conc))
ev = [(e.type, e.payload.get("branch_id")) for e in rt.store.for_mission(m.id) if e.type.startswith("Branch") or e.type.startswith("Join")]
st = fold_fork(rt.store.for_mission(m.id), "f1").to_dict()
for k in ("success_order",): st.pop(k)
for b in st["branches"]:
    for f in ("idempotency_key","receipt","result","node_id"): b.pop(f)
started = [e[1] for e in ev if e[0]=="BranchStarted"]
print(json.dumps({"started": started, "state": st, "serial_log": ev if conc==1 else None}, sort_keys=True))
"""


@pytest.mark.parametrize("conc", [1, 4])
def test_scheduling_is_deterministic_across_hash_seeds(conc):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outs = set()
    for seed in ("0", "1", "7", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": root}
        r = subprocess.run([sys.executable, "-c", _DET, str(conc)], cwd=root, env=env, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-800:]
        outs.add(r.stdout.strip().splitlines()[-1])
    assert len(outs) == 1
    d = json.loads(outs.pop())
    assert d["started"][:min(conc, 6)] == [f"b{i}" for i in range(min(conc, 6))] or conc > 1
    if conc == 1:
        assert d["started"] == [f"b{i}" for i in range(6)]    # plan order


def test_resource_key_conflicts_serialize_but_read_only_branches_overlap():
    rt, op, m = build(op=CountingOperator(delay=0.05))
    brs = [BranchSpec(f"k{i}", "w.keyed", inputs={"tag": f"k{i}", "acct": "A1"}) for i in range(3)]
    rt.forks.run_fork(m.id, ForkSpec("fk", brs, JoinSpec("all")))
    assert op.peak == 1                                     # same concurrency_key -> exclusive
    rt, op, m = build(op=CountingOperator(delay=0.05))
    brs = [BranchSpec(f"r{i}", "w.read", inputs={"tag": f"r{i}"}) for i in range(3)]
    rt.forks.run_fork(m.id, ForkSpec("fr", brs, JoinSpec("all")))
    assert op.peak >= 2


def test_per_operator_limit_honoured():
    rt, op, m = build(op=CountingOperator(delay=0.04))
    rt.policy.per_operator_limit["w-op"] = 1
    rt.forks.run_fork(m.id, spec(n=4))
    assert op.peak == 1


# ───────────────────────── join semantics ─────────────────────────
def _terminal_before_join(rt, mid, fid="f1"):
    evs = rt.store.for_mission(mid)
    join = next(e for e in evs if e.type in ("JoinSatisfied", "JoinFailed"))
    for e in evs:
        if e.type in ("BranchSucceeded", "BranchFailed", "BranchCancelled") and not e.payload.get("late"):
            assert e.seq < join.seq, "outcome persisted after the join was decided"


def test_all_join_satisfied_publishes_results_in_plan_order_after_persisting():
    rt, op, m = build(concurrency=4)
    brs = [BranchSpec(f"b{i}", "w.write", inputs={"tag": f"b{i}"}, produces=f"out{i}") for i in range(3)]
    res = rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all")))
    assert res.status == "satisfied" and list(res.results) == ["b0", "b1", "b2"]
    _terminal_before_join(rt, m.id)
    obs = [e.payload["key"] for e in rt.store.for_mission(m.id) if e.type == "ObservationWritten"]
    assert obs == ["out0", "out1", "out2"]
    assert all(v == 1 for v in op.effects.values()) and len(op.effects) == 3


def test_all_join_failure_keeps_sibling_work_and_does_not_compensate():
    rt, op, m = build(concurrency=4)
    op.fail_caps = set()
    op.fail_once = set()
    # b1 fails; b0/b2 are slower so they are in flight when b1's failure decides the join
    op.by_tag_delay = {"b0": 0.08, "b2": 0.08}
    brs = [BranchSpec("b0", "w.write", inputs={"tag": "b0"}, produces="o0"),
           BranchSpec("b1", "w.flaky", inputs={"tag": "b1", "acct": "z"}),
           BranchSpec("b2", "w.write", inputs={"tag": "b2"}, produces="o2")]
    op.fail_caps = {"w.flaky"}
    res = rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all")))
    assert res.status == "failed"
    st = rt.forks.state(m.id, "f1")
    assert st.ids("succeeded") == ["b0", "b2"] and st.ids("failed") == ["b1"]       # sibling work preserved
    assert all(b.result for b in st.ordered() if b.status == "succeeded")
    _terminal_before_join(rt, m.id)
    t = types(rt, m.id)
    assert "NodeCompensated" not in t and "NodeCompensationFailed" not in t
    assert not any(k.startswith("w.undo") for k in op.calls)
    assert m.state == MissionState.FAILED and "ObservationWritten" not in t        # nothing published
    out = rt.repo.timeline(m.id)[-1]
    assert m.outcome["fork"]["succeeded"] == ["b0", "b2"] and m.outcome["fork"]["failed"] == ["b1"]
    assert m.outcome["failure_decision"] == "halt_and_report"


@pytest.mark.parametrize("mode,k,fails,expect,winners", [
    ("any", None, {"b0", "b1"}, "satisfied", ["b2"]),
    ("any", None, {"b0", "b1", "b2"}, "failed", None),
    ("quorum", 2, {"b2"}, "satisfied", ["b0", "b1"]),
    ("quorum", 2, {"b1", "b2"}, "failed", None),
    ("quorum", 3, {"b0"}, "failed", None),
    ("all", None, {"b2"}, "failed", None),
    ("all", None, set(), "satisfied", ["b0", "b1", "b2"]),
])
def test_join_matrix(mode, k, fails, expect, winners):
    rt, op, m = build(concurrency=1)                    # serial -> deterministic completion order
    brs = [BranchSpec(f"b{i}", "w.flaky" if f"b{i}" in fails else "w.write",
                      inputs={"tag": f"b{i}", "acct": f"a{i}"}) for i in range(3)]
    op.fail_caps = {"w.flaky"}
    res = rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec(mode, k)))
    assert res.status == expect
    _terminal_before_join(rt, m.id)
    if winners is not None:
        j = next(e for e in rt.store.for_mission(m.id) if e.type == "JoinSatisfied").payload
        assert j["winners"] == winners
    else:
        assert m.state == MissionState.FAILED
    assert all(v == 1 for v in op.effects.values())


def test_any_join_cancels_losers_but_keeps_late_receipt_visible():
    rt, op, m = build(concurrency=4)
    gate = threading.Event()
    op.gates["b1"] = gate
    res = rt.forks.run_fork(m.id, spec(n=2, mode="any"))
    assert res.status == "satisfied" and res.state.ids("cancelled") == ["b1"]
    gate.set()
    for _ in range(500):
        if rt.forks.state(m.id, "f1").late_results:
            break
        time.sleep(0.004)
    st = rt.forks.state(m.id, "f1")
    assert st.branches["b1"].status == "cancelled" and len(st.late_results) == 1   # effect happened: visible
    assert st.close_payload["winners"] == ["b0"]


def test_any_join_without_cancel_losers_drains_inflight_work():
    rt, op, m = build(concurrency=4, op=CountingOperator())
    op.by_tag_delay = {"b1": 0.05}
    res = rt.forks.run_fork(m.id, spec(n=2, mode="any", cancel_losers=False))
    assert res.status == "satisfied" and res.state.ids("succeeded") == ["b0", "b1"]
    assert res.state.close_payload["winners"] == ["b0"]


class NotApplied(RuntimeError):
    not_applied = True


def _flaky_once(op, exc):
    calls = {"n": 0}
    orig = op.invoke

    def flaky(operator, capability, inputs, key, *, secrets=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return orig(operator, capability, inputs, key)
    op.invoke = flaky


def test_retry_uses_new_attempt_key_for_non_side_effecting_capability():
    rt, op, m = build()
    _flaky_once(op, RuntimeError("transient"))
    brs = [BranchSpec("b0", "w.read", inputs={"tag": "b0"}, max_attempts=2)]
    assert rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all"))).status == "satisfied"
    keys = [e.payload["idempotency_key"] for e in rt.store.for_mission(m.id) if e.type == "BranchStarted"]
    assert keys == [branch_idempotency_key(m.id, "f1", "b0", 1), branch_idempotency_key(m.id, "f1", "b0", 2)]


def test_side_effecting_branch_is_not_retried_after_a_failed_call_unless_not_applied():
    rt, op, m = build()
    _flaky_once(op, RuntimeError("timeout after send"))      # the effect may have been applied
    brs = [BranchSpec("b0", "w.write", inputs={"tag": "b0"}, max_attempts=3)]
    assert rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all"))).status == "failed"
    assert len(types(rt, m.id, "BranchStarted")) == 1 and m.state == MissionState.FAILED
    assert m.outcome["not_reversible"][0]["reversibility"] == "UNKNOWN_EFFECT"      # reported, not rolled back
    rt, op, m = build()
    _flaky_once(op, NotApplied("refused before send"))
    assert rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all"))).status == "satisfied"
    assert len(types(rt, m.id, "BranchStarted")) == 2


# ───────────────────────── failure policy hook ─────────────────────────
def test_default_is_halt_and_report_and_hook_can_only_tighten():
    seen = []

    def hook(ctx, dec):
        seen.append((ctx, dec))
        return FailureDecision("compensate", "agent wants to undo b0", compensate_nodes=("x",))   # looser: ignored
    rt, op, m = build()
    rt.forks.on_branch_failure(hook)
    op.fail_caps = {"w.flaky"}
    brs = [BranchSpec("b0", "w.write", inputs={"tag": "b0"}), BranchSpec("b1", "w.flaky", inputs={"tag": "b1"})]
    rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all")))
    ctx, dec = seen[0]
    assert [b["branch_id"] for b in ctx.succeeded] == ["b0"] and [b["branch_id"] for b in ctx.failed] == ["b1"]
    assert ctx.succeeded[0]["node_id"] == branch_node_id(m.id, "f1", "b0") and ctx.succeeded[0]["receipt"]
    j = next(e for e in rt.store.for_mission(m.id) if e.type == "JoinFailed").payload
    assert j["decision"]["action"] == "halt_and_report" and j["failure_policy"] == "HALT_AND_REPORT"
    assert "CompensationRequested" not in types(rt, m.id) and "NodeCompensated" not in types(rt, m.id)
    assert m.state == MissionState.FAILED


def test_mission_policy_continue_leaves_mission_running_and_hook_errors_are_ignored():
    rt, op, m = build(failure_policy="CONTINUE_SIBLINGS")
    rt.forks.on_branch_failure(lambda ctx, dec: 1 / 0)
    op.fail_caps = {"w.flaky"}
    brs = [BranchSpec("b0", "w.flaky", inputs={"tag": "b0"})]
    assert rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all"))).status == "failed"
    assert m.state == MissionState.RUNNING                # 'continue' leaves the mission to the caller


def test_spec_may_only_choose_a_stricter_failure_policy():
    rt, op, m = build(failure_policy="COMPENSATE_DECLARED")
    s = lambda p: ForkSpec("f1", [BranchSpec("b0", "w.write", inputs={"tag": "b0"})], JoinSpec(),  # noqa: E731
                           stricter_failure_policy=p)
    with pytest.raises(ForkError, match="may only choose"):
        rt.forks.run_fork(m.id, s("CONTINUE_SIBLINGS"))
    assert rt.forks.run_fork(m.id, s("HALT_AND_REPORT")).status == "satisfied"
    opened = next(e for e in rt.store.for_mission(m.id) if e.type == "ForkOpened").payload
    assert opened["failure_policy"] == "HALT_AND_REPORT"


# ───────────────────────── cancellation ─────────────────────────
ERRORS: list = []


def _guard(fn):
    def run():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 - surfaced by the asserting caller
            ERRORS.append(e)
    return run


def _wait_for(pred, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.003)
    return False


def test_mission_cancel_propagates_to_running_branches_and_nothing_new_starts():
    rt, op, m = build(concurrency=2)
    gate = threading.Event()
    op.gates.update({"b0": gate, "b1": gate})
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", rt.forks.run_fork(m.id, spec(n=4, max_concurrent=2))))
    t.start()
    assert _wait_for(lambda: len(types(rt, m.id, "BranchStarted")) == 2)
    rt.cancel_mission(m.id, "operator cancelled")
    t.join(5)
    st = rt.forks.state(m.id, "f1")
    assert st.closed == "failed" and st.close_payload["reason"] == "mission_cancelled"
    assert st.ids("cancelled") == ["b0", "b1", "b2", "b3"]
    assert len(types(rt, m.id, "BranchStarted")) == 2 and m.state == MissionState.CANCELLED
    gate.set()
    assert _wait_for(lambda: len(rt.forks.state(m.id, "f1").late_results) == 2)   # in-flight effects stay visible
    assert len(op.effects) == 2


def test_mission_cancel_while_parked_closes_fork_and_gate():
    rt, op, m = build()
    brs = [BranchSpec("g", "w.gated", inputs={"tag": "g"})]
    assert rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec())).status == "parked"
    assert rt.repo.pending_human(m.id)["node_id"] == branch_node_id(m.id, "f1", "g")
    rt.cancel_mission(m.id)
    assert rt.forks.state(m.id, "f1").closed == "failed" and rt.repo.pending_human(m.id) is None
    assert op.calls == {}


def test_cancel_branch_does_not_cancel_siblings_unless_policy_says_so():
    for csbc in (False, True):
        rt, op, m = build(concurrency=3)
        gate = threading.Event()
        op.gates.update({"b0": gate, "b1": gate, "b2": gate})
        s = spec(n=3, mode="quorum", k=1, cancel_siblings_on_branch_cancel=csbc, cancel_losers=False)
        t = threading.Thread(target=lambda: rt.forks.run_fork(m.id, s))
        t.start()
        assert _wait_for(lambda: len(types(rt, m.id, "BranchStarted")) == 3)
        rt.forks.cancel_branch(m.id, "f1", "b0", "operator")
        time.sleep(0.05)
        st = rt.forks.state(m.id, "f1")
        if csbc:
            assert st.ids("cancelled") == ["b0", "b1", "b2"]
        else:
            assert st.ids("cancelled") == ["b0"] and st.ids("running") == ["b1", "b2"]
        gate.set()
        t.join(5)


# ───────────────────────── budget ─────────────────────────
def test_over_budget_branch_is_refused_not_partially_run():
    rt, op, m = build(fork_budget=0.55)
    brs = [BranchSpec(f"b{i}", "w.pricey", inputs={"tag": f"b{i}"}) for i in range(3)]
    res = rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("quorum", 2)))
    assert res.status == "satisfied"
    st = res.state
    assert st.branches["b2"].status in ("failed", "cancelled")
    refused = [e for e in rt.store.for_mission(m.id) if e.type == "BranchFailed"
               and e.payload["reason"] == "budget_exceeded"]
    for e in refused:
        assert e.payload["ran"] is False and branch_idempotency_key(m.id, "f1", e.payload["branch_id"], 1) not in op.calls
    # strict ALL join: the refused branch fails the join, the runs that happened stay recorded
    rt2, op2, m2 = build(fork_budget=0.55)
    res2 = rt2.forks.run_fork(m2.id, ForkSpec("f1", brs, JoinSpec("all")))
    assert res2.status == "failed" and res2.state.branches["b2"].reason == "budget_exceeded"
    assert len(op2.effects) == 2 and res2.state.ids("succeeded") == ["b0", "b1"]


def test_shared_budget_is_atomic_under_thread_races():
    for _ in range(5):
        rt, op, m = build(fork_budget=0.55, concurrency=4, op=CountingOperator(delay=0.01))
        threads = []
        for f in ("fa", "fb", "fc"):
            brs = [BranchSpec(f"b{i}", "w.write", inputs={"tag": f"{f}{i}"}) for i in range(3)]
            s = ForkSpec(f, brs, JoinSpec("quorum", 1, cancel_losers=False))
            threads.append(threading.Thread(target=_guard(lambda s=s: rt.forks.run_fork(m.id, s))))
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        assert not ERRORS, ERRORS
        reserved = sum(e.payload["reserved"].get("usd", 0) for e in rt.store.for_mission(m.id)
                       if e.type == "BranchStarted")
        assert reserved <= 0.55 + 1e-9 and len(types(rt, m.id, "BranchStarted")) <= 5
        assert len(op.effects) == len(types(rt, m.id, "BranchStarted"))       # reserved == ran (no partial runs)


def test_budget_counts_completed_plan_nodes():
    rt, op, m = build(fork_budget=0.1)
    rt.store.append("NodeSucceeded", m.id, {"node_id": rt._plans[m.id].graph.nodes[0].id, "result": {}})
    rt._plans[m.id].graph.nodes[0].cost = NodeCost(usd=0.09)
    res = rt.forks.run_fork(m.id, spec(n=1))
    assert res.state.branches["b0"].reason == "budget_exceeded" and op.calls == {}


# ───────────────────────── timeout ─────────────────────────
def test_join_timeout_fails_and_cancels_stragglers():
    rt, op, m = build(concurrency=2)
    gate = threading.Event()
    op.gates["b1"] = gate
    res = rt.forks.run_fork(m.id, spec(n=2, timeout_s=0.15))
    gate.set()
    assert res.status == "failed" and res.state.close_payload["reason"] == "join_timeout"
    assert res.state.branches["b0"].status == "succeeded" and res.state.branches["b1"].reason == "join_timeout"


def test_join_timeout_partial_is_satisfied_only_with_a_success():
    rt, op, m = build(concurrency=2)
    gate = threading.Event()
    op.gates["b1"] = gate
    res = rt.forks.run_fork(m.id, spec(n=2, timeout_s=0.15, on_timeout="partial"))
    gate.set()
    assert res.status == "satisfied" and res.state.close_payload["partial"] is True
    rt2, op2, m2 = build(concurrency=2)
    g2 = threading.Event()
    op2.gates.update({"b0": g2, "b1": g2})
    res2 = rt2.forks.run_fork(m2.id, spec(n=2, timeout_s=0.1, on_timeout="partial"))
    g2.set()
    assert res2.status == "failed"


def test_mission_deadline_bounds_the_join():
    rt, op, m = build(concurrency=1)
    gate = threading.Event()
    op.gates["b0"] = gate
    m.deadline = time.time() + 0.1
    res = rt.forks.run_fork(m.id, spec(n=1))
    gate.set()
    assert res.status == "failed" and res.state.close_payload["reason"] == "mission_deadline"


# ───────────────────────── approvals ─────────────────────────
def test_gated_branch_parks_visibly_and_only_the_mission_approve_path_releases_it():
    rt, op, m = build()
    brs = [BranchSpec("ok", "w.write", inputs={"tag": "ok"}),
           BranchSpec("g1", "w.gated", inputs={"tag": "g1", "_approval": {"approved": True}}),
           BranchSpec("g2", "w.gated", inputs={"tag": "g2"})]
    s = ForkSpec("f1", brs, JoinSpec("all"))
    res = rt.forks.run_fork(m.id, s)
    assert res.status == "parked" and m.state == MissionState.WAITING_HUMAN
    assert op.seen_inputs and all("_approval" not in i for i in op.seen_inputs)   # forged marker stripped
    st = rt.forks.state(m.id, "f1")
    assert st.ids("parked") == ["g1", "g2"] and st.ids("succeeded") == ["ok"]
    n1, n2 = branch_node_id(m.id, "f1", "g1"), branch_node_id(m.id, "f1", "g2")
    assert rt.repo.pending_human(m.id)["node_id"] == n1
    rt.approve(m.id, n1, "approve")                                 # approving g1 does not release g2
    st = rt.forks.state(m.id, "f1")
    assert st.branches["g1"].status == "succeeded" and st.branches["g2"].status == "parked"
    assert any(i.get("_approval") == {"approved": True} and i["tag"] == "g1" for i in op.seen_inputs)
    assert not any(i["tag"] == "g2" for i in op.seen_inputs)
    rt.approve(m.id, n2, "approve")
    assert rt.forks.state(m.id, "f1").closed == "satisfied" and m.state == MissionState.RUNNING
    g = [e.payload for e in rt.store.for_mission(m.id) if e.type == "ApprovalGranted"]
    assert [x["node_id"] for x in g] == [n1, n2] and all(x["capability"] == "w.gated" for x in g)


def test_no_pre_approval_of_an_unparked_branch():
    rt, op, m = build()
    brs = [BranchSpec("g", "w.gated", inputs={"tag": "g"})]
    s = ForkSpec("f1", brs, JoinSpec())
    rt.forks._open(m, s)                                   # opened, nothing parked yet
    rt.approve(m.id, branch_node_id(m.id, "f1", "g"), "approve")
    assert "ApprovalGranted" not in types(rt, m.id)
    assert rt.forks.run_fork(m.id, s).status == "parked" and op.calls == {}


def test_rejected_branch_fails_without_saga_and_join_decides():
    rt, op, m = build()
    brs = [BranchSpec("a", "w.write", inputs={"tag": "a"}), BranchSpec("g", "w.gated", inputs={"tag": "g"})]
    rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("any")))
    # ANY already satisfied by 'a'; the parked gate was closed by cancel_losers
    assert rt.forks.state(m.id, "f1").closed == "satisfied" and rt.repo.pending_human(m.id) is None
    rt, op, m = build()
    s = ForkSpec("f1", [BranchSpec("a", "w.write", inputs={"tag": "a"}), BranchSpec("g", "w.gated", inputs={"tag": "g"})],
                 JoinSpec("all"))
    assert rt.forks.run_fork(m.id, s).status == "parked"
    rt.approve(m.id, branch_node_id(m.id, "f1", "g"), "reject")
    st = rt.forks.state(m.id, "f1")
    assert st.closed == "failed" and st.branches["g"].reason == "approval_rejected" and st.ids("succeeded") == ["a"]
    assert "NodeCompensated" not in types(rt, m.id) and m.state == MissionState.FAILED


def test_policy_tightening_invalidates_unstarted_approved_branch():
    rt, op, m = build()
    s = ForkSpec("f1", [BranchSpec("g", "w.gated", inputs={"tag": "g"})], JoinSpec())
    rt.forks.run_fork(m.id, s)
    rt.forks._specs.clear()                                # approval recorded, fork not resumed yet
    node = branch_node_id(m.id, "f1", "g")
    rt.approve(m.id, node, "approve")
    assert rt.forks.state(m.id, "f1").branches["g"].approval == "w.gated"
    rt.change_policy(m.id, actor="sec", reason="tighten", revoke=["books:write"])
    assert any(e.type == "ApprovalInvalidated" and e.payload["node_id"] == node for e in rt.store.for_mission(m.id))
    assert rt.forks.state(m.id, "f1").branches["g"].approval is None
    assert rt.forks.run_fork(m.id, s).status == "parked" and op.calls == {}      # must be re-approved


def test_revoked_capability_grant_fails_unstarted_branch_closed():
    rt, op, m = build(concurrency=1)
    gate = threading.Event()
    op.gates["b0"] = gate
    s = ForkSpec("f1", [BranchSpec("b0", "w.read", inputs={"tag": "b0"}), BranchSpec("b1", "w.write", inputs={"tag": "b1"})],
                 JoinSpec("all"), max_concurrent=1)
    t = threading.Thread(target=lambda: rt.forks.run_fork(m.id, s))
    t.start()
    assert _wait_for(lambda: len(types(rt, m.id, "BranchStarted")) == 1)
    rt.change_policy(m.id, actor="sec", reason="tighten", revoke=["w:write"])
    gate.set()
    t.join(5)
    st = rt.forks.state(m.id, "f1")
    assert st.branches["b1"].reason == "policy_revoked" and branch_idempotency_key(m.id, "f1", "b1", 1) not in op.calls


def test_mission_inputs_never_reach_branches():
    rt, op, m = build()
    m.inputs = {"secret_arg": "S"}
    m.selected_capability = "w.write"
    rt.forks.run_fork(m.id, spec(n=1))
    assert all("secret_arg" not in i for i in op.seen_inputs)


# ───────────────────────── existing folds ─────────────────────────
def test_existing_folds_ignore_fork_events_and_mission_completes_normally():
    rt, op, m = build()
    rt.forks.run_fork(m.id, spec(n=2))
    assert rt.repo.pending_human(m.id) is None
    assert not any(":fork:" in k for k in rt.repo.node_status(m.id))
    rt.run(m.id)
    while m.state == MissionState.WAITING_HUMAN:
        rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert m.state == MissionState.SUCCEEDED


def test_spec_drift_on_resume_is_refused():
    rt, op, m = build()
    rt.forks.run_fork(m.id, spec(n=2))
    with pytest.raises(ForkError, match="spec_digest"):
        rt.forks.run_fork(m.id, spec(n=3))


def test_run_fork_is_idempotent_on_a_closed_fork():
    rt, op, m = build()
    s = spec(n=3)
    a = rt.forks.run_fork(m.id, s)
    n = len(rt.store.for_mission(m.id))
    b = rt.forks.run_fork(m.id, s)
    assert len(rt.store.for_mission(m.id)) == n and a.state.to_dict() == b.state.to_dict()
    assert all(c == 1 for c in op.calls.values())


# ───────────────────────── replay + crash recovery ─────────────────────────
def test_replay_equality_via_reload_and_fold_forks(tmp_path):
    p = str(tmp_path / "log.jsonl")
    rt, op, m = build(store=EventStore(p), concurrency=4)
    rt.forks.run_fork(m.id, spec(n=5))
    live = fold_forks(rt.store.for_mission(m.id))["f1"].to_dict()
    again = fold_forks(EventStore(p).for_mission(m.id))["f1"].to_dict()
    assert live == again and live["closed"] == "satisfied"
    # fold is a pure function: folding the same list twice, or as plain tuples, agrees
    tup = [(e.type, e.payload) for e in EventStore(p).for_mission(m.id)]
    assert fold_fork(tup, "f1").to_dict() == live


def _scenarios():
    yield "all_ok", dict(n=3, mode="all"), set()
    yield "quorum", dict(n=3, mode="quorum", k=2), set()
    yield "all_fail", dict(n=3, mode="all"), {"b1"}
    yield "any", dict(n=3, mode="any"), {"b0"}


@pytest.mark.parametrize("crash_mode", ["before", "after"])
@pytest.mark.parametrize("name,kw,fails", list(_scenarios()))
def test_crash_at_every_event_boundary_resumes_without_duplicate_effects(tmp_path, crash_mode, name, kw, fails):
    def make(path, store_cls, op):
        store = store_cls(path)
        rt, _, m = build(store=store, op=op, concurrency=2)
        return rt, m, store

    def mkspec():
        s = spec(**kw)
        for b in s.branches:
            if b.branch_id in fails:
                b.capability = "w.flaky"
                b.inputs["acct"] = b.branch_id
        return s

    # baseline (no crash) -> expected final fold + number of fork-time events
    bop = CountingOperator(delay=0.002)
    bop.fail_caps = {"w.flaky"}
    brt, bm, bstore = make(str(tmp_path / "base.jsonl"), lambda p: CrashingStore(p, crash_mode), bop)
    bstore.arm(10 ** 9)
    brt.forks.run_fork(bm.id, mkspec())
    total = bstore.appended
    base = fold_forks(brt.store.for_mission(bm.id))["f1"].to_dict()
    for b in base["branches"]:
        b.pop("receipt", None)
    base_status = {b["branch_id"]: b["status"] for b in base["branches"]}
    assert total >= 5

    for n in range(1, total + 1):
        path = str(tmp_path / f"c{crash_mode}{n}.jsonl")
        op = CountingOperator(delay=0.002)
        op.fail_caps = {"w.flaky"}
        rt, m, store = make(path, lambda p: CrashingStore(p, crash_mode), op)
        store.arm(n)
        crashed = False
        try:
            rt.forks.run_fork(m.id, mkspec())
        except SimulatedCrash:
            crashed = True
        assert crashed or store.appended <= n      # event count varies with completion order
        # restart: brand-new runtime on the reloaded durable log; operator ledger (external system) persists
        store2 = EventStore(path)
        rt2, _, _ = build(store=store2, op=op, concurrency=2)
        rt2._missions.clear()
        rt2.rehydrate(m.id, policy_refs=GRANTS)
        res = rt2.forks.run_fork(m.id, mkspec())
        st = res.state.to_dict()
        got = {b["branch_id"]: b["status"] for b in st["branches"]}
        if name == "all_ok":
            assert got == base_status
        else:       # completion order under concurrency legitimately varies who is drained vs cancelled
            assert all(got[b] == "failed" for b in fails) if name == "all_fail" else True
            assert all(v in ("succeeded", "cancelled", "failed") for v in got.values())
        assert st["closed"] == base["closed"], (name, crash_mode, n)
        joins = [e for e in store2.for_mission(m.id) if e.type in ("JoinSatisfied", "JoinFailed")]
        assert len(joins) == 1
        # no duplicate consequential action: every key's real effect happened at most once
        assert all(v == 1 for v in op.effects.values()), (name, crash_mode, n, op.effects)
        # a branch with a recorded terminal outcome before the crash is never re-executed
        started = [(e.payload["branch_id"], e.payload["attempt"]) for e in store2.for_mission(m.id)
                   if e.type == "BranchStarted" and not e.payload.get("resumed")]
        assert len(started) == len(set(started))
        # fold of the recovered log equals itself after another full reload (replay equality)
        assert fold_forks(EventStore(path).for_mission(m.id))["f1"].to_dict() == fold_forks(
            store2.for_mission(m.id))["f1"].to_dict()
        if st["closed"] == "failed":
            assert rt2._missions[m.id].state == MissionState.FAILED


def test_crash_during_publish_republishes_idempotently(tmp_path):
    path = str(tmp_path / "p.jsonl")
    store = CrashingStore(path, "after")
    rt, op, m = build(store=store)
    brs = [BranchSpec("a", "w.write", inputs={"tag": "a"}, produces="o_a"),
           BranchSpec("b", "w.write", inputs={"tag": "b"}, produces="o_b")]
    s = ForkSpec("f1", brs, JoinSpec("all"))
    # crash right after JoinSatisfied is persisted (before any world publication)
    base = len(store.all())
    store.arm(1)
    store.countdown = None
    orig = store.append

    def crash_on_join(type, mid, payload):
        ev = orig(type, mid, payload)
        if type == "JoinSatisfied":
            raise SimulatedCrash("after join")
        return ev
    store.append = crash_on_join
    with pytest.raises(SimulatedCrash):
        rt.forks.run_fork(m.id, s)
    assert "ObservationWritten" not in [e.type for e in EventStore(path).for_mission(m.id)]
    rt2, _, _ = build(store=EventStore(path), op=op)
    rt2._missions.clear()
    rt2.rehydrate(m.id, policy_refs=GRANTS)
    rt2.forks.run_fork(m.id, s)
    rt2.forks.run_fork(m.id, s)                          # and again: still exactly once
    obs = [e.payload["key"] for e in rt2.store.for_mission(m.id) if e.type == "ObservationWritten"]
    assert obs == ["o_a", "o_b"] and all(v == 1 for v in op.effects.values())


def test_restart_does_not_reexecute_completed_branches(tmp_path):
    path = str(tmp_path / "r.jsonl")
    rt, op, m = build(store=EventStore(path))
    s = spec(n=3)
    rt.forks.run_fork(m.id, s)
    before = dict(op.calls)
    rt2, _, _ = build(store=EventStore(path), op=op)
    rt2._missions.clear()
    rt2.rehydrate(m.id, policy_refs=GRANTS)
    rt2.forks.run_fork(m.id, s)
    assert op.calls == before


# ───────────────────────── Python <-> Go golden parity ─────────────────────────
def _golden_cases():
    def opened(fid, n, mode="all", k=None):
        return ("ForkOpened", {"fork_id": fid, "spec_digest": "d", "join": {"mode": mode, "k": k},
                               "branches": [{"branch_id": f"b{i}", "node_id": f"m:fork:{fid}:b{i}",
                                             "capability": "w.write", "operator": "w-op"} for i in range(n)]})

    def b(t, fid, bid, **kw):
        return (t, {"fork_id": fid, "branch_id": bid, **kw})

    return [
        {"name": "all_satisfied", "fork_id": "f1", "events": [
            opened("f1", 2),
            b("BranchStarted", "f1", "b0", attempt=1, idempotency_key="k0"),
            b("BranchStarted", "f1", "b1", attempt=1, idempotency_key="k1"),
            b("BranchSucceeded", "f1", "b1", attempt=1, result={"x": 1}, receipt={"r": 1}),
            b("BranchSucceeded", "f1", "b0", attempt=1, result={"x": 0}, receipt={"r": 0}),
            ("JoinSatisfied", {"fork_id": "f1", "winners": ["b0", "b1"], "partial": False})]},
        {"name": "failed_with_late_and_retry", "fork_id": "f2", "events": [
            opened("f2", 3, "quorum", 2),
            b("BranchStarted", "f2", "b0", attempt=1, idempotency_key="a"),
            b("BranchFailed", "f2", "b0", attempt=1, reason="operator_error", ran=True, will_retry=True),
            b("BranchStarted", "f2", "b0", attempt=2, idempotency_key="b"),
            b("BranchSucceeded", "f2", "b0", attempt=2, result={"ok": 1}, receipt={}),
            b("BranchStarted", "f2", "b1", attempt=1, idempotency_key="c"),
            b("BranchCancelled", "f2", "b1", reason="join_failed", was_running=True),
            b("BranchFailed", "f2", "b2", attempt=1, reason="budget_exceeded", ran=False),
            ("JoinFailed", {"fork_id": "f2", "reason": "quorum", "decision": {"action": "halt_and_report"}}),
            b("BranchSucceeded", "f2", "b1", attempt=1, result={"late": 1}, receipt={"l": 1}, late=True),
            b("BranchSucceeded", "f2", "b2", attempt=1, result={}, receipt={})]},
        {"name": "approval_park_resume_invalidate", "fork_id": "f3", "events": [
            opened("f3", 2),
            ("NodeParked", {"node_id": "m:fork:f3:b0", "capability": "w.write"}),
            ("ApprovalGranted", {"node_id": "m:fork:f3:b0", "capability": "w.write"}),
            ("NodeResumed", {"node_id": "m:fork:f3:b0"}),
            ("ApprovalInvalidated", {"node_id": "m:fork:f3:b0"}),
            ("NodeParked", {"node_id": "m:fork:f3:b1", "capability": "w.write"}),
            ("NodeParked", {"node_id": "not-a-branch"}),
            ("JoinSatisfied", {"fork_id": "other"})]},
    ]


def _real_controller_case():
    """A real log from the controller (serial, one failure, one gated branch parked then approved), with the
    random bits (mission id, hashes, timestamps, owners) normalised so the fixture is stable."""
    import re
    rt, op, m = build(concurrency=1)
    op.fail_caps = {"w.flaky"}
    brs = [BranchSpec("b0", "w.write", inputs={"tag": "b0"}), BranchSpec("g", "w.gated", inputs={"tag": "g"}),
           BranchSpec("b2", "w.flaky", inputs={"tag": "b2"})]
    s = ForkSpec("f1", brs, JoinSpec("all"), max_concurrent=1)
    rt.forks.run_fork(m.id, s)
    rt.approve(m.id, branch_node_id(m.id, "f1", "g"), "approve")
    raw = json.dumps([{"type": e.type, "payload": e.payload} for e in rt.store.for_mission(m.id)
                      if e.type in ("ForkOpened", "ForkDriverClaimed", "ForkDriverReleased", "BranchStarted",
                                    "BranchSucceeded", "BranchFailed", "BranchCancelled", "JoinSatisfied",
                                    "JoinFailed", "NodeParked", "NodeResumed", "NodeSkipped", "ApprovalGranted")])
    raw = raw.replace(m.id, "mission_fixed")
    seen: dict = {}
    raw = re.sub(r"fj-[0-9a-f]{32}|[0-9a-f]{64}|task_[0-9a-f]+|apd_[0-9a-f]+",
                 lambda mo: seen.setdefault(mo.group(0), f"h{len(seen)}"), raw)
    evs = json.loads(raw)
    for e in evs:
        for k in ("opened_at", "lease_expires_at", "deadline"):
            if k in e["payload"]:
                e["payload"][k] = 0
        for k in ("owner", "host", "pid"):
            if k in e["payload"]:
                e["payload"][k] = "x"
        if e["type"] == "NodeParked":
            e["payload"] = {k: v for k, v in e["payload"].items() if k in ("node_id", "capability", "fork_id", "branch_id")}
        if e["type"] == "ForkOpened":
            e["payload"]["limits"] = {}
    return {"name": "real_controller_log", "fork_id": "f1", "events": [(e["type"], e["payload"]) for e in evs]}


def test_golden_fixture_is_current_and_python_fold_matches():
    cases = []
    for c in _golden_cases() + [_real_controller_case()]:
        evs = [(t, p) for t, p in c["events"]]
        cases.append({"name": c["name"], "fork_id": c["fork_id"],
                      "events": [{"type": t, "payload": p} for t, p in evs],
                      "expected": json.loads(json.dumps(fold_fork(evs, c["fork_id"]).to_dict()))})
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "go", "mission", "testdata", "fork_join_golden.json")
    rendered = json.dumps(cases, indent=1, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_FORK_GOLDEN") == "1":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(rendered)
    assert open(path).read() == rendered, "regenerate with UPDATE_FORK_GOLDEN=1"
    by = {c["name"]: c["expected"] for c in cases}
    assert by["failed_with_late_and_retry"]["closed"] == "failed" and len(by["failed_with_late_and_retry"]["late_results"]) == 2
    assert by["approval_park_resume_invalidate"]["branches"][1]["status"] == "parked"
