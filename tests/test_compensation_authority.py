"""Compensation is its own authority, evaluated when the undo is about to run.

An undo is a side effect. The forward grant does not imply it: the owner's envelope must hold a grant on ``compensate:<capability>``
(a bare ``*`` forward grant does not cover it), or, outside production, the explicit test policy must list the capability under
``compensate``. When compensation is denied, or done for some steps only, the log and the outcome say so, name every effect that
remains, and ``rolled_back`` stays false. Already-applied steps can still be compensated by an explicit, authenticated operator
action (``MissionRuntime.compensate``) that needs its own permission, ``mission.<scope>.compensate``."""
from __future__ import annotations

import pytest

from agentic_os.evidence_query.authz import StaticCeiling
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger)
from agentic_os.mission.authority_policy import AuthorityTestPolicy
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.planner import ExecutionIntent, IntentStep
from agentic_os.mission.principal import PrincipalError
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime, ResumeNotAuthorized
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, MissionState
from tests._principals import authn

NOW = 9_000_000.0
FWD_ONLY = AuthorityTestPolicy("forward-only", "may run the steps, may not undo them", allow=("*",), compensate=(), anonymous_operators=True)


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


def specs():
    return [_spec("res.a", "a", side_effecting=True, compensation={"supported": True, "capability": "res.undo_a", "verify": "res.verify"}),
            _spec("res.undo_a", None, side_effecting=True, irreversible=True),
            _spec("res.b", "b", side_effecting=True, compensation={"supported": True, "capability": "res.undo_b", "verify": "res.verify"}),
            _spec("res.undo_b", None, side_effecting=True, irreversible=True),
            _spec("res.verify", None),
            _spec("crm.finish", "finished", side_effecting=True, irreversible=True)]


class NotApplied(RuntimeError):
    not_applied = True                       # the provider says nothing was applied: a definite failure, not an unknown outcome


class World:
    def __init__(self):
        self.undone: list[str] = []
        self.on_finish = lambda: None

    def handlers(self):
        def undo(tag):
            def h(i):
                self.undone.append(tag)
                return {"undone": True, "executed": True}
            return h

        def finish(i):
            self.on_finish()
            raise NotApplied("downstream failed")
        return {"res.a": lambda i: {"executed": True, "a": 1}, "res.b": lambda i: {"executed": True, "b": 1},
                "res.undo_a": undo("a"), "res.undo_b": undo("b"),
                "res.verify": lambda i: {"verified": True, "observed": {"gone": True}}, "crm.finish": finish}


def build(*, policy=None, guard=None, world=None, run=True, **kw):
    world = world or World()
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", specs()))
    extra = {}
    if guard is not None:
        extra.update(authority=guard, authority_key=lambda m: "env-1")
    rt = MissionRuntime(reg, Executor(InMemoryOperatorClient(world.handlers())), planner=_Planner("a", "b", "finished"),
                        test_policy=policy, store=kw.pop("store", None) or EventStore(), **extra)
    m = rt.create_mission("go", policy_refs=["*"], failure_policy="COMPENSATE_DECLARED", tenant="acme" if guard is not None else "")
    if run:
        rt.run(m.id)
    return rt, m, world


def envelope(*grants):
    return AuthorityEnvelope(tenant="acme", principal="owner", mission_id="env-1", issued_at=NOW - 10, expires_at=NOW + 3600,
                             grants=grants, budget=AutonomyBudget(usd=100, max_actions=100))


def guard_with(*grants):
    store = MemoryEnvelopeStore()
    store.put(envelope(*grants))
    return AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1), store


def ev(rt, mid, t):
    return [e.payload for e in rt.store.for_mission(mid) if e.type == t]


# ── automatic compensation needs its own, current authority ─────────────────────────────────────────────────────────
def test_a_test_policy_without_a_compensation_scope_denies_the_undo_and_says_what_remains():
    rt, m, w = build(policy=FWD_ONLY)
    assert w.undone == []                                                         # the undo never reached an operator
    denied = ev(rt, m.id, "CompensationDenied")
    assert {d["capability"] for d in denied} == {"res.a", "res.b"} and all(d["rule"] == "no_authority" and d["reason"] for d in denied)
    assert m.state is MissionState.COMPENSATION_FAILED
    o = m.outcome
    assert o["rolled_back"] is False and o["compensation_incomplete"] is True
    assert len(o["compensation_denied"]) == 2 and o["compensation_state"] == "denied"
    assert {r["reason"] for r in o["retained_effects"] if r["capability"] in ("res.a", "res.b")} == {"compensation_not_authorized"}


def test_a_test_policy_that_lists_the_capabilities_under_compensate_allows_the_undo():
    pol = AuthorityTestPolicy("fwd-and-undo", "may run and undo", allow=("*",), compensate=("res.*",))
    rt, m, w = build(policy=pol)
    assert sorted(w.undone) == ["a", "b"] and not ev(rt, m.id, "CompensationDenied")
    used = [p for p in ev(rt, m.id, "TestPolicyAuthorized") if p["phase"] == "compensate"]
    assert {p["capability"] for p in used} == {"res.a", "res.b"}
    assert m.outcome["rolled_back"] is True


def test_the_forward_grant_does_not_imply_the_undo_even_when_it_is_a_wildcard():
    guard, _ = guard_with(Grant("*"))
    rt, m, w = build(guard=guard)
    assert w.undone == [] and {d["rule"] for d in ev(rt, m.id, "CompensationDenied")} == {"out_of_scope"}
    assert m.outcome["rolled_back"] is False and m.state is MissionState.COMPENSATION_FAILED


def test_a_compensate_grant_allows_the_undo():
    guard, _ = guard_with(Grant("*"), Grant("compensate:res.a"), Grant("compensate:res.b"))
    rt, m, w = build(guard=guard)
    assert sorted(w.undone) == ["a", "b"] and not ev(rt, m.id, "CompensationDenied")
    assert {p["authority_basis"] for p in ev(rt, m.id, "CompensationAuthorized")} == {"envelope"}
    assert m.outcome["rolled_back"] is True


def test_compensation_is_evaluated_when_it_runs_not_when_the_step_was_authorized():
    guard, store = guard_with(Grant("*"), Grant("compensate:*"))
    w = World()
    w.on_finish = lambda: store.revoke("env-1")                    # the owner revokes while the mission is failing
    rt, m, w = build(guard=guard, world=w)
    assert w.undone == [] and {d["rule"] for d in ev(rt, m.id, "CompensationDenied")} == {"revoked"}
    assert m.outcome["rolled_back"] is False


def test_partial_compensation_reports_both_halves():
    pol = AuthorityTestPolicy("only-b", "may undo b only", allow=("*",), compensate=("res.b",), anonymous_operators=True)
    rt, m, w = build(policy=pol)
    assert w.undone == ["b"]
    o = m.outcome
    assert len(o["compensated"]) == 1 and len(o["compensation_denied"]) == 1 and o["rolled_back"] is False
    assert o["compensation_state"] == "partial"
    assert [d["capability"] for d in ev(rt, m.id, "CompensationDenied")] == ["res.a"]


def test_a_repeated_saga_records_the_same_denial_once():
    rt, m, w = build(policy=FWD_ONLY)
    n = len(ev(rt, m.id, "CompensationDenied"))
    rt._saga(m, rt._plans[m.id], rt._world(m.id))
    assert len(ev(rt, m.id, "CompensationDenied")) == n


# ── an explicit, authorized operator action can still compensate what was applied ─────────────────────────────────────
CEILING = StaticCeiling({"mission.default.compensate"})


def test_an_operator_with_the_compensate_permission_can_undo_applied_steps_after_a_denial():
    rt, m, w = build(policy=FWD_ONLY)
    assert w.undone == []
    rt.compensate(m.id, principal=authn("ops", tenant="*"), ceiling=CEILING, scope="default", reason="owner approved the undo")
    assert sorted(w.undone) == ["a", "b"]
    req = ev(rt, m.id, "CompensationOperatorRequested")[0]
    assert req["principal"]["id"] == "ops" and req["reason"] == "owner approved the undo" and req["scope"] == "default"
    assert {p["authority_basis"] for p in ev(rt, m.id, "CompensationAuthorized")} == {"operator:ops"}
    assert m.outcome["rolled_back"] is True and m.outcome["compensation_denied"] == [] and m.state is MissionState.FAILED


def test_an_operator_without_the_permission_is_refused_and_nothing_runs():
    from agentic_os.mission.runtime import CompensationNotAuthorized
    rt, m, w = build(policy=FWD_ONLY)
    with pytest.raises(CompensationNotAuthorized):
        rt.compensate(m.id, principal=authn("ops", tenant="*"), ceiling=StaticCeiling({"evidence.default.inspect"}), scope="default",
                      reason="owner approved the undo")
    assert w.undone == [] and ev(rt, m.id, "CompensationOperatorRefused")[-1]["reason"] == "not_authorized"


def test_an_unauthenticated_principal_cannot_compensate():
    rt, m, w = build(policy=FWD_ONLY)
    from agentic_os.overlays import Principal
    with pytest.raises(PrincipalError):
        rt.compensate(m.id, principal=Principal("ops"), ceiling=CEILING, scope="default", reason="owner approved the undo")
    with pytest.raises(PrincipalError):
        rt.compensate(m.id, principal=None, ceiling=CEILING, scope="default", reason="owner approved the undo")
    assert w.undone == []


def test_operator_compensation_needs_a_reason():
    rt, m, w = build(policy=FWD_ONLY)
    with pytest.raises(ValueError):
        rt.compensate(m.id, principal=authn("ops", tenant="*"), ceiling=CEILING, scope="default", reason="x")
    assert w.undone == []


def test_the_operator_permission_is_checked_again_for_each_step_when_it_runs():
    class Flaky(StaticCeiling):
        def __init__(self):
            super().__init__({"mission.default.compensate"})
            self.calls = 0

        def allows(self, principal, permission):
            self.calls += 1
            return self.calls <= 2 and super().allows(principal, permission)       # granted for the entry check and the first step only

    rt, m, w = build(policy=FWD_ONLY)
    rt.compensate(m.id, principal=authn("ops", tenant="*"), ceiling=Flaky(), scope="default", reason="owner approved the undo")
    assert len(w.undone) == 1 and len(m.outcome["compensation_denied"]) == 1 and m.outcome["rolled_back"] is False


def test_a_mission_rebuilt_from_the_log_is_still_read_only_for_compensation(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt, m, w = build(policy=FWD_ONLY, store=EventStore(path=path))
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", specs()))
    w2 = World()
    rt2 = MissionRuntime(reg, Executor(InMemoryOperatorClient(w2.handlers())), planner=_Planner("a", "b", "finished"), store=EventStore(path=path),
                         test_policy=FWD_ONLY)
    rt2.rehydrate(m.id)
    with pytest.raises(ResumeNotAuthorized):
        rt2.compensate(m.id, principal=authn("ops", tenant="*"), ceiling=CEILING, scope="default", reason="owner approved the undo")
    assert w2.undone == []
    rt2.grant_resume(m.id, principal=authn("ops", tenant="*"), ceiling=StaticCeiling({"evidence.default.resume"}), scope="default")
    rt2.compensate(m.id, principal=authn("ops", tenant="*"), ceiling=CEILING, scope="default", reason="owner approved the undo")
    assert sorted(w2.undone) == ["a", "b"]


def test_compensating_a_succeeded_mission_marks_it_cancelled_not_succeeded():
    class OK(World):
        def handlers(self):
            h = super().handlers()
            h["crm.finish"] = lambda i: {"executed": True, "finished": 1}
            return h
    rt, m, w = build(policy=FWD_ONLY, world=OK())
    assert m.state is MissionState.SUCCEEDED and w.undone == []
    rt.compensate(m.id, principal=authn("ops", tenant="*"), ceiling=CEILING, scope="default", reason="the customer cancelled the order")
    assert sorted(w.undone) == ["a", "b"]                                           # both reversible steps are undone; the irreversible finish stays
    assert m.state is MissionState.CANCELLED and ev(rt, m.id, "MissionCancelled")[-1]["reason"].startswith("operator compensation")
    assert m.outcome["rolled_back"] is False                                       # crm.finish is irreversible and remains
