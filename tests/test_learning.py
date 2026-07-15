"""P5 — four learning loops closed to the bus, and reflection→planning.

Headline: the closed loop measurably improves outcomes. With two providers for the same outcome,
a flaky one's confidence drops from measured failure and the compiler (via the cost model) switches
to the reliable provider — no static rule, learned from outcomes."""
from __future__ import annotations

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.learning import LearningRouter, reflect
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
)


def _charged_fleet():
    """Two providers for 'charged': A is preferred by the cost model but flaky; B is reliable."""
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("provA", [CapabilitySpec(
        "provA.charge", "provA", provides=["charged"], side_effecting=True,
        estimated_value="high", permissions=[])]))          # ranks first initially
    reg.register(CapabilityManifest("provB", [CapabilitySpec(
        "provB.charge", "provB", provides=["charged"], side_effecting=True,
        estimated_value="medium", permissions=[])]))
    client = InMemoryOperatorClient({
        "provA.charge": lambda i: (_ for _ in ()).throw(RuntimeError("gateway down")),  # always fails
        "provB.charge": lambda i: {"charge_id": "ch_1"},
    })
    return reg, client


class _ChargePlanner:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id,
                               steps=[IntentStep(outcome="charged", need="charge the customer")])


def _node_cap(rt, mid):
    return rt._plans[mid].graph.nodes[0].capability


# ─── the closed-loop benchmark: learning shifts to the reliable provider ─────
def test_capability_learning_shifts_provider_after_failure():
    reg, client = _charged_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_ChargePlanner())

    m1 = rt.create_mission("charge", policy_refs=["*"])
    assert _node_cap(rt, m1.id) == "provA.charge"       # cost model prefers A at first
    rt.run(m1.id)
    assert m1.state == MissionState.FAILED               # A is flaky → mission fails
    assert reg.get("provA.charge").confidence == 0.0     # measured failure demoted it

    m2 = rt.create_mission("charge", policy_refs=["*"])
    assert _node_cap(rt, m2.id) == "provB.charge"        # learned: switch to the reliable provider
    rt.run(m2.id)
    assert m2.state == MissionState.SUCCEEDED             # the lift — no static rule, learned

    # vs a no-learning baseline (fresh registry each time) which would keep picking A and failing
    reg2, client2 = _charged_fleet()
    base = MissionRuntime(reg2, Executor(client2), planner=_ChargePlanner(),
                          learning=LearningRouter())      # learning present, but re-prove A is chosen cold
    mb = base.create_mission("charge", policy_refs=["*"])
    assert _node_cap(base, mb.id) == "provA.charge"       # cold start still picks A


# ─── reflection proposes a concrete alternative for the failed outcome ────────
def test_reflection_proposes_better_capability():
    reg, client = _charged_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_ChargePlanner())
    m = rt.create_mission("charge", policy_refs=["*"])
    rt.run(m.id)
    refl = [e for e in rt.repo.timeline(m.id) if e["type"] == "ReflectionEmitted"][0]["payload"]
    assert refl["success"] is False and refl["better_capability"] == "provB.charge"


# ─── bus wiring: a learner replica folds the event stream off the serving path ─
def test_attach_folds_events_off_the_bus():
    reg, client = _charged_fleet()
    store = EventStore()
    replica = LearningRouter(reg)
    replica.attach(store)                                  # a separate learner consuming the bus
    rt = MissionRuntime(reg, Executor(client), store=store, planner=_ChargePlanner(),
                        learning=LearningRouter())         # runtime uses its OWN inline router
    m = rt.create_mission("charge", policy_refs=["*"]); rt.run(m.id)
    # the replica learned from the bus that provA.charge failed
    assert replica.capability_confidence("provA.charge") == 0.0
    assert "provA.charge" in replica.policy()["capability"]


# ─── the four loops are distinct in the policy view ──────────────────────────
def test_learning_policy_separates_loops():
    reg, client = _charged_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_ChargePlanner())
    rt.run(rt.create_mission("charge", policy_refs=["*"]).id)
    pol = rt.learning.policy()
    assert set(pol) == {"capability", "planning", "business", "verification"}
    assert pol["capability"]["provA.charge"] == 0.0
