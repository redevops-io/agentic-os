"""Saga-style, explicit-only compensation (docs/COMPENSATION.md). Negative cases first."""
from __future__ import annotations

import time

import pytest

from agentic_os.mission import compensation as C
from agentic_os.mission.approval import inputs_hash
from agentic_os.mission.compensation_policy import (ActionKind, FailurePolicy, decide_on_branch_failure,
                                                    effective_policy, is_forked)
from agentic_os.mission.compensation_runner import (InterventionError, attention_items, outcome_from_log)
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.operator_sdk import capability
from agentic_os.mission.planner import ExecutionIntent, IntentStep
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, MissionState, Node, NodeState

OPT_IN = "COMPENSATE_DECLARED"


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
    if kw.get("irreversible") is True:
        kw.setdefault("irreversible_note", "test fixture")
    return CapabilitySpec(name, name.split(".")[0], provides=[provides] if provides else [], permissions=["*"], **kw)


def _boom(i):
    raise RuntimeError("downstream failed")


def _rt(specs, handlers, outcomes, **mission_kw):
    mission_kw.setdefault("failure_policy", OPT_IN)          # compensation is an explicit opt-in (S6)
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


def _readback(i):
    return {"verified": True, "observed": {"reservation": None}}


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


def test_validate_plan_compensation_dangling_and_strict_sunset(monkeypatch):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", [_spec("a.do", "x", side_effecting=True, undo="a.undo")]))
    ok = Node(capability="a.do", operator="a", undo="a.undo")
    bad = Node(capability="a.do", operator="a", undo="evil")
    assert C.validate_plan_compensation([ok], reg.get) == []                    # dangling tolerated before sunset
    assert C.validate_plan_compensation([ok], reg.get, strict=True)             # ...refused when strict
    assert C.validate_plan_compensation([bad], reg.get)
    import datetime
    assert not C.strict_undo_targets(datetime.date(2026, 12, 31))
    assert C.strict_undo_targets(datetime.date(2027, 1, 1))
    monkeypatch.setenv(C.ENV_STRICT_TARGETS, "1")
    assert C.strict_undo_targets(datetime.date(2026, 1, 1))
    monkeypatch.setenv(C.ENV_STRICT_TARGETS, "0")
    assert not C.strict_undo_targets(datetime.date(2030, 1, 1))


def test_strict_flag_refuses_a_dangling_undo_plan(monkeypatch):
    monkeypatch.setenv(C.ENV_STRICT_TARGETS, "1")
    rt, m = _rt([_spec("pay.charge", "paid", side_effecting=True, undo="pay.ghost")],
                {"pay.charge": lambda i: {"executed": True, "paid": True}}, ["paid"])
    assert m.state == MissionState.FAILED and "NodeDispatched" not in _types(rt, m)


def test_unregistered_undo_target_gets_warning_and_never_the_marker():
    box, h = _calls()
    specs = [_spec("res.reserve", "reserved", side_effecting=True, undo="res.ghost"),
             _spec("crm.finish", "finished", side_effecting=True, irreversible=True)]
    rt, m = _rt(specs, {"res.reserve": lambda i: {"reserved": True, "executed": True}, "crm.finish": _boom,
                        "res.ghost": h}, ["reserved", "finished"])
    t = _types(rt, m)
    assert "CompensationUndeclaredTarget" in t and "CompensationFailed" in t
    assert box["n"] == 0                                       # the unregistered target was never invoked
    assert m.state == MissionState.COMPENSATION_FAILED
    assert "undo_not_registered" in str(rt.explain(m.id)["compensation_remaining"])


# ── B2: self-compensation / cycles / contradictory declarations ───────────────────────────────────────────────
def test_self_compensation_rejected_at_registration():
    reg = CapabilityRegistry()
    with pytest.raises(ValueError, match="itself"):
        reg.register(CapabilityManifest("m", [_spec("x.do", "d", side_effecting=True,
                                                    compensation={"supported": True, "capability": "x.do"})]))
    with pytest.raises(ValueError, match="itself"):
        reg.register(CapabilityManifest("m", [_spec("x.do", "d", side_effecting=True, undo="x.do")]))


def test_verify_equal_to_undo_rejected_at_registration():
    reg = CapabilityRegistry()
    with pytest.raises(ValueError, match="verify"):
        reg.register(CapabilityManifest("m", [_spec("x.do", "d", side_effecting=True, compensation={
            "supported": True, "capability": "x.undo", "verify": "x.undo"})]))


def test_contradictory_declarations_rejected():
    with pytest.raises(ValueError):
        C.coerce({"supported": False, "reversibility": "REVERSIBLE_EXPLICIT"})
    with pytest.raises(ValueError):
        C.coerce({"supported": False, "capability": "x.undo"})
    with pytest.raises(ValueError):
        C.coerce({"supported": True, "capability": "x.undo", "reversibility": "IRREVERSIBLE"})


def test_registry_lint_flags_nonexistent_target_cycle_bad_verify():
    a = _spec("a.do", "x", side_effecting=True, compensation={"supported": True, "capability": "a.undo",
                                                              "verify": "a.rb"})
    undo = _spec("a.undo", None, side_effecting=True, compensation={"supported": True, "capability": "a.do",
                                                                   "verify": "a.rb"})
    rb = _spec("a.rb", None, side_effecting=True)                       # a verify must be read-only
    by = {c.name: c for c in (a, undo, rb)}
    msgs = " | ".join(C.registry_problems([a, undo], by.get))
    assert "cycle" in msgs and "side-effecting" in msgs
    ghost = _spec("g.do", "x", side_effecting=True, compensation={"supported": True, "capability": "g.nope"})
    msgs = " | ".join(C.registry_problems([ghost], {}.get))
    assert "not registered" in msgs and "no `verify`" in msgs
    missing = _spec("m.do", "x", side_effecting=True, compensation={"supported": True, "capability": "m.undo",
                                                                    "verify": "m.rb"})
    assert "verify 'm.rb' is not registered" in " | ".join(C.registry_problems([missing], {"m.undo": undo}.get))


def test_executor_refuses_self_compensation_even_if_a_plan_carries_it():
    box, h = _calls()
    ex = Executor(InMemoryOperatorClient({"x.do": h}))
    ex.undo_registry = lambda cap: "x.do"
    node = Node(capability="x.do", operator="x", undo="x.do", side_effecting=True, idempotency_key="k")
    ex.run(node, {"_mission": "m1"})
    before = box["n"]
    out = ex.compensate(node, mission_id="m1")
    assert "compensation_target_is_forward_action" in out["undo_error"] and box["n"] == before


def test_runtime_refuses_undo_that_is_another_forward_action_of_the_plan():
    box, h = _calls()
    specs = [_spec("a.step", "a", side_effecting=True, undo="b.step"), _spec("b.step", "b", side_effecting=True,
                                                                          irreversible=True),
             _spec("c.fail", "c", side_effecting=True, irreversible=True)]
    rt, m = _rt(specs, {"a.step": lambda i: {"executed": True, "a": 1}, "b.step": h, "c.fail": _boom},
                ["a", "b", "c"])
    assert box["n"] == 1                                       # b ran once, forward; never re-run as an "undo"
    assert "forward_action" in str(m.outcome["compensation_remaining"]) or m.state == MissionState.COMPENSATION_FAILED


# ── failed / unverified compensation is never "rolled back" ───────────────────────────────────────────────────
def _reserve_specs(**comp):
    return [_spec("res.reserve", "reserved", side_effecting=True,
                  compensation={"supported": True, "capability": "res.release", **comp}),
            _spec("res.release", None, side_effecting=True, irreversible=True),
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


def test_B1_bare_undo_is_never_rolled_back_without_a_readback():
    """Reviewer reproduction: a legacy `undo=` capability whose undo 'succeeds' used to count as verified."""
    box, rel = _calls()
    specs = [_spec("pay.charge", "paid", side_effecting=True, undo="pay.refund"),
             _spec("pay.refund", None, side_effecting=True, irreversible=True),
             _spec("crm.finish", "finished", side_effecting=True, irreversible=True)]
    rt, m = _rt(specs, {"pay.charge": lambda i: {"paid": True, "executed": True}, "pay.refund": rel,
                        "crm.finish": _boom}, ["paid", "finished"])
    assert box["n"] == 1 and m.outcome["rolled_back"] is False
    assert m.outcome["compensated"] == [] and len(m.outcome["compensated_unverified"]) == 1
    assert "NodeCompensated" not in _types(rt, m)


@pytest.mark.parametrize("readback", [
    {"ok": True},                                     # a lying/laconic handler: no explicit verified
    {"verified": True},                               # verified but no observed state / digest
    {"verified": "yes", "observed": {"a": 1}},        # not the boolean True
    {"verified": False, "observed": {"a": 1}},
])
def test_readback_must_echo_verified_true_and_observed_state(readback):
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": lambda i: readback}),
                ["reserved", "finished"])
    assert m.state == MissionState.COMPENSATION_FAILED and m.outcome["rolled_back"] is False
    assert "NodeCompensated" not in _types(rt, m)


def test_verified_compensation_is_compensated_and_rolled_back_only_with_readback():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": _readback}), ["reserved", "finished"])
    t = _types(rt, m)
    assert "CompensationVerified" in t and "NodeCompensated" in t
    assert len(m.outcome["compensated"]) == 1 and m.outcome["compensated_unverified"] == []
    assert m.state == MissionState.FAILED and m.outcome["rolled_back"] is True


def test_manual_verification_counts_as_compensated_but_not_rolled_back():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(), _h(rel), ["reserved", "finished"])
    rt.compensation_intervention(m.id, "verify_manually", actor="alice", reason="checked the console")
    assert len(m.outcome["compensated"]) == 1 and m.outcome["rolled_back"] is False
    ev = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationVerified"][0]["payload"]
    assert ev["method"] == "manual" and ev["actor"] == "alice" and ev["reason"]


def test_double_compensation_runs_undo_once():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": _readback}), ["reserved", "finished"])
    rt._saga(m, rt._plans[m.id], rt._world(m.id))
    rt._saga(m, rt._plans[m.id], rt._world(m.id))
    assert box["n"] == 1 and _types(rt, m).count("CompensationRequested") == 1


# ── S2: attempt-scoped keys, retry, cap, lock ────────────────────────────────────────────────────────────────
def test_retry_uses_attempt_scoped_keys_and_a_soft_failing_operator_can_succeed():
    seen = []
    state = {"ok": False}

    def rel(i):
        seen.append(state["ok"])
        return {"released": True, "executed": True} if state["ok"] else {"status": "error", "error": "soft fail"}
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": _readback}), ["reserved", "finished"])
    assert m.state == MissionState.COMPENSATION_FAILED
    state["ok"] = True
    rt.compensation_intervention(m.id, "retry", actor="alice", reason="endpoint is back")
    # an idempotent operator replays a returned error forever for a REUSED key; attempt 2 has a fresh key
    keys = [k for (_, k) in rt._executor_for(m).client.calls if ":undo:" in k and not k.endswith(":verify")]
    assert [k.rsplit(":", 1)[1] for k in keys] == ["1", "2"]
    assert m.state == MissionState.FAILED and m.outcome["rolled_back"] is True
    assert "CompensationRetryRequested" in _types(rt, m)


def test_max_attempts_then_only_acknowledge(monkeypatch):
    monkeypatch.setenv(C.ENV_MAX_ATTEMPTS, "2")
    rt, m = _rt(_reserve_specs(), _h(lambda i: {"status": "error", "error": "down"}), ["reserved", "finished"])
    rt.compensation_intervention(m.id, "retry", actor="alice")
    with pytest.raises(InterventionError):
        rt.compensation_intervention(m.id, "retry", actor="alice")          # cap reached
    assert attention_items(rt, m.id)[0]["decisions"] == ["acknowledge"]
    rt.compensation_intervention(m.id, "acknowledge", actor="alice", reason="accept the residual reservation")
    assert m.state == MissionState.FAILED and m.outcome["rolled_back"] is False
    assert m.outcome["compensation_acknowledged"] and not m.outcome.get("compensation_incomplete")
    ack = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationAcknowledged"][0]["payload"]
    assert ack["actor"] == "alice" and ack["reason"] and ack["at"]


def test_intervention_in_the_wrong_state_is_refused():
    box, rel = _calls()
    rt, m = _rt([_spec("a.do", "a", side_effecting=True, irreversible=True)], {"a.do": lambda i: {"a": 1, "executed": True}}, ["a"])
    assert m.state == MissionState.SUCCEEDED
    with pytest.raises(InterventionError):
        rt.compensation_intervention(m.id, "retry", actor="x")
    assert not hasattr(rt, "retry_compensation")


# ── S3: the compensation's OWN approval ───────────────────────────────────────────────────────────────────────
def _approval_rt():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(requires_approval=True, verify="res.verify"),
                _h(rel, **{"res.verify": _readback}), ["reserved", "finished"])
    return rt, m, box


def test_requires_approval_parks_until_a_human_approves_then_runs_once():
    rt, m, box = _approval_rt()
    assert box["n"] == 0 and m.state == MissionState.COMPENSATION_FAILED
    t = _types(rt, m)
    assert "CompensationApprovalRequired" in t and "CompensationAuthorized" not in t
    item = attention_items(rt, m.id)[0]
    assert item["kind"] == "compensation_approval" and item["undo"] == "res.release" and item["inputs"] is not None
    nid = item["node_id"]
    rt.compensation_intervention(m.id, "approve", actor="approver:acme", node_id=nid)
    assert box["n"] == 1 and m.outcome["rolled_back"] is True
    appr = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationApproved"][0]["payload"]
    assert appr["approver"] == "approver:acme" and appr["attempt"] == 1 and appr["inputs_sha256"]
    auth = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationAuthorized"][0]["payload"]
    assert auth["basis"] == "own_human_approval"
    with pytest.raises(InterventionError):                                   # approved ONCE: not replayable
        rt.compensation_intervention(m.id, "approve", actor="approver:acme", node_id=nid)
    assert box["n"] == 1


def test_forward_approval_alone_never_authorises_a_requires_approval_compensation():
    ex = Executor(InMemoryOperatorClient({"x.undo": lambda i: {"ok": True, "executed": True}}))
    ex.undo_registry = lambda cap: "x.undo"
    ex.compensation_requires_approval = lambda cap: True
    node = Node(capability="x.do", operator="x", undo="x.undo", side_effecting=True, idempotency_key="k", id="n1")
    ex.evidence_probe = lambda mid, nid: {"executed": True, "capability": "x.do", "approved": True,
                                          "result": {"executed": True, "id": 7}}
    out = ex.compensate(node, mission_id="m1")
    assert out["compensation_skipped"] and "OWN human approval" in out["undo_error"]


def _good_approval(node, mid="m1", attempt=1, result=None, **over):
    from agentic_os.mission.compensation import undo_inputs
    a = {"mission_id": mid, "node_id": node.id, "undo": node.undo, "attempt": attempt,
         "inputs_sha256": inputs_hash(undo_inputs(node, mid, result)), "exp": time.time() + 600}
    a.update(over)
    return a


@pytest.mark.parametrize("over", [
    {"mission_id": "other-mission"}, {"attempt": 2}, {"exp": 1.0}, {"inputs_sha256": "0" * 64},
    {"undo": "x.other"}, {"node_id": "n2"},
])
def test_compensation_approval_replay_expired_cross_mission_modified_inputs_refused(over):
    calls = {"n": 0}
    ex = Executor(InMemoryOperatorClient({"x.undo": lambda i: calls.update(n=calls["n"] + 1) or {"executed": True}}))
    ex.undo_registry = lambda cap: "x.undo"
    ex.compensation_requires_approval = lambda cap: True
    node = Node(capability="x.do", operator="x", undo="x.undo", side_effecting=True, idempotency_key="k", id="n1")
    fwd = {"executed": True, "id": 7}
    ex.evidence_probe = lambda mid, nid: {"executed": True, "capability": "x.do", "approved": False, "result": fwd}
    out = ex.compensate(node, mission_id="m1", approval=_good_approval(node, result=fwd, **over))
    assert out.get("compensation_skipped") and calls["n"] == 0
    ok = ex.compensate(node, mission_id="m1", approval=_good_approval(node, result=fwd))
    assert calls["n"] == 1 and not ok.get("compensation_skipped")


def test_compensation_dispatch_carries_its_own_node_binding_and_attempt_key():
    seen = {}

    class Client:
        def invoke(self, operator, capability, inputs, key, **kw):
            seen.update(capability=capability, inputs=inputs, key=key)
            return {"executed": True}
    ex = Executor(Client())
    ex.undo_registry = lambda cap: "x.undo"
    ex.evidence_probe = lambda mid, nid: {"executed": True, "capability": "x.do", "approved": False,
                                          "result": {"executed": True}}
    node = Node(capability="x.do", operator="x", undo="x.undo", side_effecting=True, idempotency_key="k", id="n1")
    ex.compensate(node, mission_id="m1", attempt=3)
    assert seen["capability"] == "x.undo" and seen["key"] == "k:undo:3"
    assert seen["inputs"]["_node"] == "n1:undo" and seen["inputs"]["_approval"]["compensation"] is True


# ── S4: replay equality ──────────────────────────────────────────────────────────────────────────────────────
def test_outcome_rebuilt_from_the_log_equals_the_live_outcome():
    box, rel = _calls()
    specs = _reserve_specs(verify="res.verify") + [_spec("mail.send", "sent", side_effecting=True, irreversible=True)]
    rt, m = _rt(specs, _h(rel, **{"res.verify": _readback, "mail.send": lambda i: {"sent": True, "executed": True}}),
                ["sent", "reserved", "finished"])
    from agentic_os.mission.compensation_runner import OUTCOME_KEYS
    live = {k: v for k, v in m.outcome.items() if k in OUTCOME_KEYS}
    assert live["not_reversible"] and live["failure_policy"] == OPT_IN and live["compensated"]
    assert outcome_from_log(rt, m.id) == live
    rt2 = MissionRuntime(rt.registry, Executor(InMemoryOperatorClient({})), planner=_Planner("sent", "reserved", "finished"),
                         store=rt.store)
    m2 = rt2.rehydrate(m.id, policy_refs=["*"])
    assert m2.state == m.state
    assert {k: v for k, v in m2.outcome.items() if k in OUTCOME_KEYS} == live


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
    assert effective_policy(_M(), forked=False) is FailurePolicy.HALT_AND_REPORT      # S6: no auto-undo by default
    assert effective_policy(_M(), forked=True) is FailurePolicy.HALT_AND_REPORT


def test_default_policy_env_template_and_mission_precedence(monkeypatch):
    class T(_M):
        template = "deploy_app"
    assert effective_policy(T()) is FailurePolicy.PRESERVE_ONLY                # kernel template default: preserve
    for tpl in ("onboarding", "deploy_app", "sky_deploy"):
        class K(_M):
            template = tpl
        assert effective_policy(K()) is FailurePolicy.PRESERVE_ONLY
    class U(_M):
        template = "invoice_recovery"
    assert effective_policy(U()) is FailurePolicy.HALT_AND_REPORT
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "COMPENSATE_DECLARED")  # deployment escape hatch
    assert effective_policy(U()) is FailurePolicy.COMPENSATE_DECLARED
    assert effective_policy(T()) is FailurePolicy.COMPENSATE_DECLARED          # explicit deployment opt-in beats the table
    assert effective_policy(U(), forked=True) is FailurePolicy.HALT_AND_REPORT     # forks never inherit it
    explicit = U()
    explicit.failure_policy = "HALT_AND_REPORT"
    assert effective_policy(explicit) is FailurePolicy.HALT_AND_REPORT
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "ROLLBACK")
    assert effective_policy(U()) is FailurePolicy.HALT_AND_REPORT


def test_default_mission_compensates_nothing():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": _readback}), ["reserved", "finished"],
                failure_policy="")
    assert box["n"] == 0 and not any(t.startswith("Compensation") for t in _types(rt, m))
    assert m.outcome["not_compensated"]


def test_forks_field_is_the_single_source_for_the_fork_default():
    from agentic_os.mission.types import ExecutionGraph
    g = ExecutionGraph()
    assert g.forks == [] and not is_forked(g)
    g.forks.append("branch-a")
    assert is_forked(g)


def test_runtime_halt_and_report_policy_compensates_nothing():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": _readback}),
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
    assert C.reversibility_of(capability("mail.send", lambda i: {}, side_effecting=True, irreversible="no recall").spec) \
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
    assert C.lint_capability(capability("x.send", lambda i: {}, side_effecting=True,
                                         irreversible="sent mail cannot be recalled").spec) is None
    assert "note" in C.lint_capability(capability("x.send", lambda i: {}, side_effecting=True, irreversible=True).spec)
    assert C.lint_capability(capability("x.read", lambda i: {}).spec) is None
    both = capability("x.a", lambda i: {}, side_effecting=True, irreversible="n",
                      compensation={"supported": True, "capability": "x.b"}).spec
    assert C.lint_capability(both)


def test_fold_is_forward_compatible_and_replay_safe():
    evs = [{"type": "CompensationRequested", "payload": {"node_id": "n", "undo": "u"}},
           {"type": "SomeFutureEvent", "payload": {"node_id": "n"}},
           {"type": "CompensationExecuted", "payload": {"node_id": "n", "receipt": {"k": 1}}},
           {"type": "CompensationVerified", "payload": {"node_id": "n", "method": "readback"}}]
    assert C.fold_compensation(evs)["n"]["status"] == "compensated"
    assert C.summarize(C.fold_compensation(evs[:3]))["compensated_unverified"] == ["n"]



# ── S1: authenticated operator routes ────────────────────────────────────────────────────────────────────────
ADMIN, TOK_A, TOK_B, APPR_A = "admin-secret", "tok-a", "tok-b", "appr-a"
H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731


def _routes_fx(monkeypatch, approvers=True, tenant="acme", **kw):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.service import mount
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TOK_A},globex={TOK_B}")
    if approvers:
        monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", f"acme={APPR_A}")
    else:
        monkeypatch.delenv("AGENTIC_OS_APPROVER_TOKENS", raising=False)
    box, rel = _calls()
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", _reserve_specs(requires_approval=True, verify="res.verify")))
    rt = MissionRuntime(reg, Executor(InMemoryOperatorClient(_h(rel, **{"res.verify": _readback}))),
                        planner=_Planner("reserved", "finished"))
    m = rt.create_mission("go", policy_refs=["*"], tenant=tenant, failure_policy=OPT_IN)
    rt.run(m.id)
    while m.state == MissionState.WAITING_HUMAN:
        rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"])
    app = FastAPI()
    mount(app, rt)
    return TestClient(app), rt, m, box


def _nid(rt, m):
    return attention_items(rt, m.id)[0]["node_id"]


def test_route_approve_requires_the_approver_credential_and_records_who(monkeypatch):
    c, rt, m, box = _routes_fx(monkeypatch)
    url = f"/missions/{m.id}/compensation/approve"
    body = {"node_id": _nid(rt, m), "reason": "looked at the inputs"}
    assert c.post(url, json=body).status_code == 401                                   # anonymous
    assert c.post(url, json=body, headers=H(TOK_A)).status_code == 403                 # requester != approver
    assert c.post(url, json=body, headers=H(TOK_B)).status_code == 404                 # cross-tenant: no leak
    assert box["n"] == 0
    r = c.post(url, json=body, headers=H(APPR_A))
    assert r.status_code == 200 and box["n"] == 1
    p = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationApproved"][0]["payload"]
    assert p["approver"] == "approver:acme" and p["reason"] == "looked at the inputs"


def test_route_wrong_state_unknown_action_and_retry_denied_to_approver(monkeypatch):
    c, rt, m, box = _routes_fx(monkeypatch)
    base = f"/missions/{m.id}/compensation"
    assert c.post(f"{base}/explode", json={}, headers=H(ADMIN)).status_code == 404
    assert c.post(f"{base}/retry", json={}, headers=H(APPR_A)).status_code == 403      # approver may only approve
    assert c.post(f"{base}/retry", json={}, headers=H(ADMIN)).status_code == 409        # nothing failed to retry
    assert c.post(f"{base}/acknowledge", json={}, headers=H(TOK_A)).status_code == 403  # consequential
    assert c.post(f"{base}/acknowledge", json={"reason": "accept"}, headers=H(ADMIN)).status_code == 200
    assert c.post(f"{base}/acknowledge", json={}, headers=H(ADMIN)).status_code == 409  # already resolved
    ack = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationAcknowledged"][0]["payload"]
    assert ack["actor"] == "admin" and ack["reason"] == "accept"


def test_route_enumeration_includes_the_compensation_route_behind_the_gate(monkeypatch):
    c, rt, m, _ = _routes_fx(monkeypatch)
    paths = [getattr(r, "path", "") for r in c.app.routes]
    from tests.test_mission_api_default_deny import _all_routes
    routes = _all_routes(c.app)
    comp = [r for r in routes if r.path == "/missions/{mid}/compensation/{action}"]
    assert comp and "POST" in comp[0].methods
    assert any(getattr(d, "dependency", None) for d in comp[0].dependencies)           # gate attached


# ── re-verify fixes ──────────────────────────────────────────────────────────────────────────────────────────
def _failed_comp_rt():
    return _rt(_reserve_specs(), _h(lambda i: {"status": "error", "error": "gateway"}), ["reserved", "finished"])


def test_generic_approve_on_a_terminal_mission_is_refused_and_does_not_reenter_the_saga():
    from agentic_os.mission.runtime import ApprovalError
    rt, m = _failed_comp_rt()
    assert m.state == MissionState.COMPENSATION_FAILED
    before = len(rt.repo.timeline(m.id))
    for decision in ("reject", "approve"):
        with pytest.raises(ApprovalError):
            rt.approve(m.id, "bogus-node", decision)
    assert len(rt.repo.timeline(m.id)) == before and m.state == MissionState.COMPENSATION_FAILED
    assert _types(rt, m).count("MissionOutcome") == 1


def test_approve_unknown_node_on_a_waiting_mission_is_refused():
    from agentic_os.mission.runtime import ApprovalError
    rt, m = _rt([_spec("a.do", "a", side_effecting=True, irreversible=True)], {"a.do": lambda i: {"a": 1, "executed": True}}, ["a"])
    m2 = rt.create_mission("two", policy_refs=["*"])
    rt.run(m2.id)
    if m2.state == MissionState.WAITING_HUMAN:
        with pytest.raises(ApprovalError):
            rt.approve(m2.id, "not-the-pending-node", "reject")
        assert m2.state == MissionState.WAITING_HUMAN


def test_cancel_is_refused_while_compensation_is_parked_or_failed():
    rt, m, box = _approval_rt()
    assert m.state == MissionState.COMPENSATION_FAILED
    with pytest.raises(InterventionError):
        rt.cancel_mission(m.id, "oops")
    assert m.state == MissionState.COMPENSATION_FAILED and attention_items(rt, m.id)
    rt.compensation_intervention(m.id, "acknowledge", actor="admin", node_id=attention_items(rt, m.id)[0]["node_id"])


def test_duplicated_compensation_requested_does_not_bump_attempts():
    ev = [{"type": C.EV_REQUESTED, "payload": {"node_id": "n", "undo": "u", "attempt": 1}}] * 3
    assert C.fold_compensation(ev)["n"]["attempts"] == 1
    ev2 = ev + [{"type": C.EV_REQUESTED, "payload": {"node_id": "n", "undo": "u", "attempt": 2}}]
    assert C.fold_compensation(ev2)["n"]["attempts"] == 2


def test_default_failure_policy_is_frozen_at_creation(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "COMPENSATE_DECLARED")
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(), _h(rel), ["reserved", "finished"], failure_policy="")
    created = [e for e in rt.repo.timeline(m.id) if e["type"] == "MissionCreated"][0]["payload"]
    assert created["default_failure_policy"] == "COMPENSATE_DECLARED"
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "HALT_AND_REPORT")
    assert effective_policy(m) is FailurePolicy.COMPENSATE_DECLARED


def test_failed_mission_with_kept_effects_raises_a_needs_you_item():
    box, rel = _calls()
    rt, m = _rt(_reserve_specs(verify="res.verify"), _h(rel, **{"res.verify": _readback}),
                ["reserved", "finished"], failure_policy="HALT_AND_REPORT")
    it = [i for i in attention_items(rt, m.id) if i["kind"] == "effects_left_in_place"]
    assert it and it[0]["not_compensated"]


def test_forward_approver_may_not_approve_compensation_when_several_approvers(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=a1,globex=a2")
    rt, m, box = _approval_rt()
    nid = _nid(rt, m)
    rt.store.append("ApprovalGranted", m.id, {"node_id": nid, "approver": "approver:acme"})
    with pytest.raises(InterventionError):
        rt.compensation_intervention(m.id, "approve", actor="approver:acme", node_id=nid)
    rt.compensation_intervention(m.id, "approve", actor="approver:globex", node_id=nid)
    assert box["n"] == 1
