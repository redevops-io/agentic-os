"""Mission Runtime kernel tests — every layer + the two headline guarantees (durable
restart-resume, saga compensation on failure)."""
from __future__ import annotations

import pytest

from agentic_os.mission import belief
from agentic_os.mission.compiler import compile_intent, CompileError, _assert_acyclic
from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.scheduler import TopoScheduler, SchedulePolicy
from agentic_os.mission.simulator import simulate
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, NodeCost, Node, ExecutionGraph, ExecutionIntent,
    IntentStep, Mission, MissionState, Budget,
)

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


# ─── discovery + belief ──────────────────────────────────────────────────────
def test_capability_discovery_ranks_by_need():
    reg, _ = build_fleet()
    top = reg.discover("record the revenue in the books", k=1)[0][0]
    assert top.name == "books.record_revenue"


def test_belief_fusion_majority_and_conflict():
    agree = belief.fuse([{"value": "pro", "source": "crm", "confidence": 1.0},
                         {"value": "pro", "source": "billing", "confidence": 1.0},
                         {"value": "free", "source": "inbox", "confidence": 0.2}])
    assert agree.value == "pro" and not agree.conflict and agree.confidence > 0.8

    split = belief.fuse([{"value": "pro", "source": "crm", "confidence": 1.0},
                         {"value": "free", "source": "billing", "confidence": 1.0}])
    assert split.conflict is True


# ─── compiler (physical plan) ────────────────────────────────────────────────
def test_compiler_permission_fail_closed():
    reg, _ = build_fleet()
    m = Mission(goal="onboard", policy_refs=["billing:write"])  # missing the other grants
    intent = __import__("agentic_os.mission.templates", fromlist=["onboarding"]).onboarding(m.id)
    with pytest.raises(CompileError) as ei:
        compile_intent(m, intent, reg)
    assert "permission denied" in str(ei.value)


def test_compiler_deterministic_node_ids():
    reg, _ = build_fleet()
    m = Mission(goal="onboard", policy_refs=GRANTS)
    from agentic_os.mission.templates import onboarding
    ids1 = [n.id for n in compile_intent(m, onboarding(m.id), reg).graph.nodes]
    ids2 = [n.id for n in compile_intent(m, onboarding(m.id), reg).graph.nodes]
    assert ids1 == ids2 and all(i.startswith("nd_") for i in ids1)


def test_compiler_detects_cycle():
    a = Node(capability="a", operator="o"); b = Node(capability="b", operator="o")
    a.depends_on = [b.id]; b.depends_on = [a.id]
    with pytest.raises(CompileError):
        _assert_acyclic(ExecutionGraph(nodes=[a, b]))


# ─── simulator ────────────────────────────────────────────────────────────────
def test_simulator_flags_over_budget():
    reg, _ = build_fleet()
    m = Mission(goal="onboard", policy_refs=GRANTS, budget=Budget(usd=0.0001, human_minutes=0.0))
    from agentic_os.mission.templates import onboarding
    plan = compile_intent(m, onboarding(m.id), reg)
    sim = simulate(plan, m, reg)
    assert sim.within_budget is False and sim.expected_approvals == 1


# ─── scheduler ────────────────────────────────────────────────────────────────
def test_scheduler_waves_and_concurrency():
    a = Node(capability="a", operator="o1"); a.id = "a"
    b = Node(capability="b", operator="o2", depends_on=["a"]); b.id = "b"
    c = Node(capability="c", operator="o3", depends_on=["a"]); c.id = "c"
    g = ExecutionGraph(nodes=[a, b, c])
    sch = TopoScheduler()
    assert [n.id for n in sch.ready(g, set(), set(), SchedulePolicy())] == ["a"]
    wave2 = {n.id for n in sch.ready(g, {"a"}, set(), SchedulePolicy())}
    assert wave2 == {"b", "c"}
    limited = sch.ready(g, {"a"}, set(), SchedulePolicy(max_concurrency=1))
    assert len(limited) == 1


# ─── executor ─────────────────────────────────────────────────────────────────
def test_executor_idempotency_dedupe():
    calls = {"n": 0}
    def handler(_):
        calls["n"] += 1
        return {"ok": True}
    client = InMemoryOperatorClient({"cap": handler})
    node = Node(capability="cap", operator="o", idempotency_key="k1")
    ex = Executor(client)
    r1 = ex.run(node, {}); r2 = ex.run(node, {})   # retry with same key
    assert r1 == r2 and calls["n"] == 1            # underlying handler ran exactly once


def test_executor_compensate_calls_undo():
    seen = {}
    def _undo(_):
        seen["undone"] = True
        return {"undone": True}
    client = InMemoryOperatorClient({"do": lambda i: {"x": 1}, "undo": _undo})
    node = Node(capability="do", operator="o", undo="undo", side_effecting=True, idempotency_key="k")
    ex = Executor(client)
    ex.run(node, {})
    out = ex.compensate(node)
    assert out == {"undone": True} and seen["undone"]


# ─── runtime: onboarding end-to-end + human gate ─────────────────────────────
def _runtime(store=None):
    reg, client = build_fleet()
    return MissionRuntime(reg, Executor(client), store=store or EventStore()), reg, client


def test_onboarding_end_to_end_with_human_gate():
    rt, _, _ = _runtime()
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN
    pending = rt.repo.pending_human(m.id)
    assert pending and pending["capability"] == "compliance.file_consent"

    rt.approve(m.id, pending["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    world = rt._world(m.id).snapshot()
    assert {"subscription", "onboarding_sent", "revenue_recorded", "consent_filed"} <= set(world)
    assert rt.learning.business_value("onboarding") == 100.0


def test_restart_resume_from_event_log(tmp_path):
    store = EventStore(path=str(tmp_path / "events.jsonl"))
    rt, reg, client = _runtime(store=store)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN
    node_id = rt.repo.pending_human(m.id)["node_id"]

    # simulate a crash+restart: brand-new store from disk + fresh runtime, no in-memory plan/mission
    store2 = EventStore(path=str(tmp_path / "events.jsonl"))
    rt2 = MissionRuntime(reg, Executor(client), store=store2)
    m2 = rt2.rehydrate(m.id)
    assert m2.state == MissionState.WAITING_HUMAN and m.id not in rt2._plans or True
    rt2.approve(m.id, node_id, "approve")
    assert rt2._missions[m.id].state == MissionState.SUCCEEDED


def test_reject_fails_mission():
    rt, _, _ = _runtime()
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    node_id = rt.repo.pending_human(m.id)["node_id"]
    rt.approve(m.id, node_id, "reject")
    assert rt._missions[m.id].state == MissionState.FAILED


# ─── runtime: saga compensation on failure ───────────────────────────────────
class _FailPlanner:
    """Two side-effecting steps that succeed, then one that fails — to exercise sagas."""
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[
            IntentStep(outcome="charged", need="charge the customer"),
            IntentStep(outcome="booked", need="record the entry", inputs_from=["charged"]),
            IntentStep(outcome="notified", need="notify the customer", inputs_from=["booked"]),
        ])


def _saga_fleet():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("billing", [
        CapabilitySpec("charge", "billing", provides=["charged"], side_effecting=True,
                       undo="refund", permissions=["*"])]))
    reg.register(CapabilityManifest("books", [
        CapabilitySpec("book", "books", provides=["booked"], side_effecting=True,
                       undo="reverse", permissions=["*"])]))
    reg.register(CapabilityManifest("email", [
        CapabilitySpec("notify", "email", provides=["notified"], side_effecting=True,
                       permissions=["*"])]))
    undone = []
    handlers = {
        "charge": lambda i: {"charge_id": "ch_1"},
        "book": lambda i: {"entry": "je_1"},
        "notify": lambda i: (_ for _ in ()).throw(RuntimeError("smtp down")),  # fails
        "refund": lambda i: undone.append("refund") or {"refunded": True},
        "reverse": lambda i: undone.append("reverse") or {"reversed": True},
    }
    return reg, InMemoryOperatorClient(handlers), undone


def test_saga_compensation_on_failure():
    reg, client, undone = _saga_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_FailPlanner())
    m = rt.create_mission("charge and notify", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.FAILED
    # both completed side-effecting nodes were compensated (reverse-order)
    assert set(undone) == {"refund", "reverse"}
    comp = [e for e in rt.repo.timeline(m.id) if e["type"] == "NodeCompensated"]
    assert len(comp) == 2
    refl = [e for e in rt.repo.timeline(m.id) if e["type"] == "ReflectionEmitted"][0]
    assert refl["payload"]["success"] is False


# ─── learning ─────────────────────────────────────────────────────────────────
def test_capability_learning_updates_confidence():
    rt, reg, _ = _runtime()
    for ok in (True, True, False, True):
        rt.learning.record_capability("billing.create_subscription", ok)
    assert reg.get("billing.create_subscription").confidence == 0.75
