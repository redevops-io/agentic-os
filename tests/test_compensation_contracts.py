"""Saga-style, explicit-only compensation (docs/COMPENSATION.md). Negative cases first."""
from __future__ import annotations

import pytest

from agentic_os.mission import compensation as C
from agentic_os.mission.compensation_policy import (ActionKind, FailurePolicy, decide_on_branch_failure,
                                                    effective_policy)
from agentic_os.mission.compensation_runner import attention_items
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.operator_sdk import capability
from agentic_os.mission.planner import ExecutionIntent, IntentStep
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, MissionState, Node, NodeState


class _Planner:
    def __init__(self, *outcomes):
        self.outcomes = outcomes

    def plan(self, mission_id, goal, context):
        steps, prev = [], []
        for o in self.outcomes:
            steps.append(IntentStep(outcome=o, need=f"do {o}", inputs_from=list(prev)))
            prev = [o]
        return ExecutionIntent(mission_id=mission_id, steps=steps)


def _spec(name, provides, **kw):
    return CapabilitySpec(name, name.split(".")[0], provides=[provides] if provides else [], permissions=["*"], **kw)


def _boom(i):
    raise RuntimeError("downstream failed")


def _rt(specs, handlers, outcomes, **mission_kw):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", specs))
    rt = MissionRuntime(reg, Executor(InMemoryOperatorClient(handlers)), planner=_Planner(*outcomes))
    m = rt.create_mission("go", policy_refs=["*"], **mission_kw)
    rt.run(m.id)
    while m.state == MissionState.WAITING_HUMAN:           # unknown/irreversible steps park for a human: approve
        rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"])
    return rt, m


def _types(rt, m):
    return [e["type"] for e in rt.repo.timeline(m.id)]


def _calls():
    box = {"n": 0}

    def h(i):
        box["n"] += 1
        return {"ok": True, "executed": True}
    return box, h


# ── negative: irreversible / unknown steps are never compensated ─────────────────────────────────────────────
def test_irreversible_step_never_compensated_and_listed_not_reversible():
    box, undo_h = _calls()
    specs = [_spec("mail.send", "sent", side_effecting=True, irreversible=True),
             _spec("pay.submit", "paid", side_effecting=True),                      # UNKNOWN -> irreversible
             _spec("crm.fail", "failed", side_effecting=True, irreversible=True)]
    rt, m = _rt(specs, {"mail.send": lambda i: {"sent": True, "executed": True},
                        "pay.submit": lambda i: {"paid": True, "executed": True},
                        "crm.fail": _boom, "mail.undo": undo_h}, ["sent", "paid", "failed"])
    assert m.state == MissionState.FAILED
    t = _types(rt, m)
    assert not any(x.startswith("Compensation") or x in ("NodeCompensated", "NodeCompensationFailed") for x in t)
    nr = {e["capability"]: e["reversibility"] for e in m.outcome["not_reversible"]}
    assert nr == {"mail.send": "IRREVERSIBLE", "pay.submit": "UNKNOWN"}
    assert m.outcome["rolled_back"] is False and box["n"] == 0


def test_undeclared_undo_refused_at_plan_time(monkeypatch):
    from agentic_os.mission import plan_select
    real = plan_select.compile_intent

    def tampered(*a, **k):
        plan = real(*a, **k)
        for n in plan.graph.nodes:
            n.undo = "attacker.wipe"          # planner-attached undo the registry never declared
        return plan
    monkeypatch.setattr(plan_select, "compile_intent", tampered)
    box, h = _calls()
    rt, m = _rt([_spec("pay.charge", "paid", side_effecting=True, undo="pay.refund"), _spec("pay.refund", None)],
                {"pay.charge": lambda i: {"executed": True, "paid": True}, "attacker.wipe": h}, ["paid"])
    assert m.state == MissionState.FAILED and box["n"] == 0
    assert "NodeDispatched" not in _types(rt, m)
    blocked = [e for e in rt.repo.timeline(m.id) if e["type"] == "MissionBlocked"][0]
    assert blocked["payload"]["reason"] == "compensation_undeclared"


def test_validate_plan_compensation_and_dangling():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", [_spec("a.do", "x", side_effecting=True, undo="a.undo")]))
    ok = Node(capability="a.do", operator="a", undo="a.undo")
    bad = Node(capability="a.do", operator="a", undo="evil")
    assert C.validate_plan_compensation([ok], reg.get) == []
    assert C.validate_plan_compensation([bad], reg.get)
    assert C.dangling_undos(reg.all(), reg.get)           # a.undo is declared but not registered


# ── failed / unverified compensation is never "rolled back" ───────────────────────────────────────────────────
def _reserve_specs(**comp):
    return [_spec("res.reserve", "reserved", side_effecting=True,
                  compensation={"supported": True, "capability": "res.release", **comp}),
            _spec("res.release", None, side_effecting=True),
            _spec("res.verify", None),
            _spec("crm.finish", "finished", side_effecting=True, irreversible=True)]


def _h(release, **extra):
    return {"res.reserve": lambda i: {"reserved": True, "executed": True}, "res.release": release,
            "crm.finish": _boom, **extra}


def test_failed_compensation_is_compensation_failed_not_rolled_back():
    rt, m = _rt(_reserve_specs(), _h(lambda i: {"status": "error", "error": "gateway"}), ["reserved", "finished"])
    assert m.state == MissionState.COMPENSATION_FAILED
    t = _types(rt, m)
    for ev in ("CompensationRequested", "CompensationAuthorized", "CompensationFailed", "NodeCompensationFailed"):
        assert ev in t
    assert "CompensationVerified" not in t and "NodeCompensated" not in t
    assert m.outcome["rolled_back"] is False and m.outcome["compensation_incomplete"] is True
    items = attention_items(rt, m.id)
    assert items and items[0]["kind"] == "compensation_failed" and "retry_compensation" in items[0]["decisions"]
    assert rt.explain(m.id)["compensation"]["compensation_failed"]


def test_unverified_compensation_not_reported_compensated():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(), _h(rel), ["reserved", "finished"])       # no `verify` declared
    assert box["n"] == 1
    t = _types(rt, m)
    assert "CompensationExecuted" in t and "CompensationVerified" not in t and "NodeCompensated" not in t
    assert m.outcome["compensated"] == [] and len(m.outcome["compensated_unverified"]) == 1
    assert m.outcome["rolled_back"] is False
    assert [i["kind"] for i in attention_items(rt, m.id)] == ["compensation_unverified"]
    assert rt.repo.node_status(m.id)[m.outcome["compensated_unverified"][0]] != NodeState.COMPENSATED


def test_verified_compensation_is_compensated():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": lambda i: {"verified": True}}),
                ["reserved", "finished"])
    t = _types(rt, m)
    assert "CompensationVerified" in t and "NodeCompensated" in t
    assert len(m.outcome["compensated"]) == 1 and m.outcome["compensated_unverified"] == []
    assert m.state == MissionState.FAILED
    # the irreversible final step is NOT in `compensated`; it failed itself (never executed forward)


def test_failed_readback_verification_is_compensation_failed():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": lambda i: {"verified": False}}),
                ["reserved", "finished"])
    assert m.state == MissionState.COMPENSATION_FAILED
    assert "verification_failed" in rt.explain(m.id)["compensation_remaining"][m.outcome["compensation_remaining"][0]]


def test_double_compensation_runs_undo_once_and_retry_only_failed():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": lambda i: {"verified": True}}),
                ["reserved", "finished"])
    rt._saga(m, rt._plans[m.id], rt._world(m.id))
    rt._saga(m, rt._plans[m.id], rt._world(m.id))
    assert box["n"] == 1 and _types(rt, m).count("CompensationRequested") == 1


def test_retry_compensation_after_failure_moves_back_to_failed():
    state = {"ok": False}

    def rel(i):
        if not state["ok"]:
            raise RuntimeError("release endpoint down")
        return {"released": True, "executed": True}
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": lambda i: {"verified": True}}),
                ["reserved", "finished"])
    assert m.state == MissionState.COMPENSATION_FAILED
    state["ok"] = True
    rt.retry_compensation(m.id)
    assert m.state == MissionState.FAILED
    assert len(m.outcome["compensated"]) == 1 and "compensation_incomplete" not in m.outcome


def test_requires_approval_refused_without_forward_human_approval():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(requires_approval=True), _h(rel), ["reserved", "finished"])
    assert box["n"] == 0                                   # the undo was never invoked
    assert m.state == MissionState.COMPENSATION_FAILED
    assert "compensation_requires_approval" in str(rt.explain(m.id)["compensation_remaining"])
    assert "CompensationAuthorized" not in _types(rt, m)


# ── #447 guard stays: compensation without evidence is refused ───────────────────────────────────────────────
def test_compensation_without_evidence_refused():
    box, h = _calls()
    ex = Executor(InMemoryOperatorClient({"x.undo": h}))
    ex.undo_registry = lambda cap: "x.undo"
    node = Node(capability="x.do", operator="x", undo="x.undo", side_effecting=True, idempotency_key="k")
    out = ex.compensate(node, mission_id="m1")
    assert out["compensation_skipped"] and box["n"] == 0


# ── failure policy: sibling successes untouched unless COMPENSATE_DECLARED ───────────────────────────────────
def _branch(bid, status, *nodes):
    return {"id": bid, "status": status, "nodes": list(nodes)}


def _n(i, undo=None, se=True, irr=False):
    return {"id": i, "capability": f"c.{i}", "side_effecting": se, "undo": undo, "irreversible": irr}


class _M:
    failure_policy = ""


def test_halt_and_report_leaves_succeeded_siblings_untouched():
    sibs = [_branch("b", "succeeded", _n("n1", undo="c.undo1")), _branch("c", "succeeded", _n("n2", irr=True))]
    act = decide_on_branch_failure(_M(), _branch("a", "failed"), sibs)
    assert act.policy is FailurePolicy.HALT_AND_REPORT and act.kind is ActionKind.HALT
    assert act.compensate_nodes == () and act.not_compensated == ("n1",)
    assert [x["node_id"] for x in act.not_reversible] == ["n2"]


def test_compensate_declared_compensates_only_declared_undos():
    m = _M()
    m.failure_policy = "compensate_declared"
    sibs = [_branch("b", "succeeded", _n("n1", undo="c.u1"), _n("n3", undo="c.u3")),
            _branch("c", "succeeded", _n("n2", irr=True), _n("n4", se=False)), _branch("d", "running", _n("n5", undo="c.u5"))]
    act = decide_on_branch_failure(m, _branch("a", "failed"), sibs)
    assert act.kind is ActionKind.COMPENSATE and act.compensate_nodes == ("n3", "n1")   # reverse; running sibling excluded
    assert [x["node_id"] for x in act.not_reversible] == ["n2"]


def test_continue_siblings_compensates_nothing_and_does_not_cancel():
    m = _M()
    m.failure_policy = "CONTINUE_SIBLINGS"
    act = decide_on_branch_failure(m, _branch("a", "failed"), [_branch("b", "succeeded", _n("n1", undo="u"))])
    assert act.kind is ActionKind.CONTINUE and act.compensate_nodes == () and act.cancel_siblings is False


def test_unknown_policy_fails_safe_and_defaults_by_topology():
    m = _M()
    m.failure_policy = "ROLLBACK_EVERYTHING"
    assert effective_policy(m, forked=False) is FailurePolicy.HALT_AND_REPORT
    assert effective_policy(_M(), forked=False) is FailurePolicy.COMPENSATE_DECLARED
    assert effective_policy(_M(), forked=True) is FailurePolicy.HALT_AND_REPORT


def test_runtime_halt_and_report_policy_compensates_nothing():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": lambda i: {"verified": True}}),
                ["reserved", "finished"], failure_policy="HALT_AND_REPORT")
    assert box["n"] == 0 and not any(t.startswith("Compensation") for t in _types(rt, m))
    assert m.outcome["not_compensated"] and m.outcome["rolled_back"] is False
    assert m.state == MissionState.FAILED


def test_failure_policy_survives_rehydrate():
    rt, m = _rt(_reserve_specs(), _h(lambda i: {"released": True, "executed": True}), ["reserved", "finished"],
                failure_policy="halt_and_report")
    created = [e for e in rt.repo.timeline(m.id) if e["type"] == "MissionCreated"][0]
    assert created["payload"]["failure_policy"] == "HALT_AND_REPORT"


# ── capability metadata + lint ───────────────────────────────────────────────────────────────────────────────
def test_sdk_capability_declares_compensation_and_derives_undo():
    c = capability("res.reserve", lambda i: {}, side_effecting=True,
                   compensation={"supported": True, "capability": "res.release", "notes": "release reservation"})
    assert c.spec.undo == "res.release"
    assert C.reversibility_of(c.spec) is C.Reversibility.REVERSIBLE_EXPLICIT
    assert C.reversibility_of(capability("mail.send", lambda i: {}, side_effecting=True, irreversible=True).spec) \
        is C.Reversibility.IRREVERSIBLE
    assert C.reversibility_of(capability("x.read", lambda i: {}).spec) is C.Reversibility.NOT_APPLICABLE


def test_compensation_dict_validation_fails_closed():
    with pytest.raises(ValueError):
        C.coerce({"supported": True})                       # supported without a compensating capability
    with pytest.raises(ValueError):
        C.coerce({"supported": False, "mystery": 1})        # unknown key
    with pytest.raises(ValueError):
        C.coerce({"reversibility": "MAYBE"})


def test_lint_catches_undeclared_consequential_capability():
    bad = capability("x.send", lambda i: {}, side_effecting=True).spec
    assert C.lint_capability(bad) and "neither" in C.lint_capability(bad)
    assert C.lint_capability(capability("x.send", lambda i: {}, side_effecting=True, irreversible=True).spec) is None
    assert C.lint_capability(capability("x.read", lambda i: {}).spec) is None
    both = capability("x.a", lambda i: {}, side_effecting=True, irreversible=True,
                      compensation={"supported": True, "capability": "x.b"}).spec
    assert C.lint_capability(both)


def test_fold_is_forward_compatible_and_replay_safe():
    evs = [{"type": "CompensationRequested", "payload": {"node_id": "n", "undo": "u"}},
           {"type": "SomeFutureEvent", "payload": {"node_id": "n"}},
           {"type": "CompensationExecuted", "payload": {"node_id": "n", "receipt": {"k": 1}}},
           {"type": "CompensationVerified", "payload": {"node_id": "n", "method": "readback"}}]
    assert C.fold_compensation(evs)["n"]["status"] == "compensated"
    assert C.summarize(C.fold_compensation(evs[:3]))["compensated_unverified"] == ["n"]

