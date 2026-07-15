"""P4 — belief-driven disambiguation. Observations are not facts: when a required world-state
input is conflicting or low-confidence, the mission is gated for resolution, not silently trusted."""
from __future__ import annotations

from agentic_os.mission import belief
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
)


def _spec(name, op, provides):
    return CapabilityManifest(op, [CapabilitySpec(name, op, provides=provides)])


class _Planner:
    def __init__(self, steps): self._steps = steps
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=self._steps())


# ─── belief fusion: reliability + conflict ───────────────────────────────────
def test_single_uncertain_source_is_low_confidence():
    b = belief.fuse([{"value": "gold", "source": "crm", "confidence": 0.5}])
    assert b.value == "gold" and b.confidence == 0.5 and not b.conflict   # reliability folded in


def test_confident_single_source_is_high():
    assert belief.fuse([{"value": "gold", "source": "crm", "confidence": 1.0}]).confidence == 1.0


# ─── conflicting sources → disambiguation gate → resolve → succeed ───────────
def test_conflicting_belief_gates_then_resolves():
    reg = CapabilityRegistry()
    reg.register(_spec("crm.pull", "crm", ["signal_a"]))
    reg.register(_spec("billing.pull", "billing", ["signal_b"]))
    reg.register(_spec("offers.make", "offers", ["offer"]))
    client = InMemoryOperatorClient({
        "crm.pull": lambda i: {"_observations": [{"key": "tier", "value": "gold", "source": "crm"}]},
        "billing.pull": lambda i: {"_observations": [{"key": "tier", "value": "silver", "source": "billing"}]},
        "offers.make": lambda i: {"offer": f"deal for {i.get('tier')}"},
    })
    steps = lambda: [
        IntentStep(outcome="signal_a", need="pull the CRM signal"),
        IntentStep(outcome="signal_b", need="pull the billing signal"),
        IntentStep(outcome="offer", need="make a tailored offer", inputs_from=["signal_a", "signal_b", "tier"]),
    ]
    rt = MissionRuntime(reg, Executor(client), planner=_Planner(steps))
    m = rt.create_mission("make a tailored offer", policy_refs=["*"])
    rt.run(m.id)

    assert m.state == MissionState.WAITING_HUMAN
    pend = rt.repo.pending_human(m.id)
    assert pend["kind"] == "disambiguation" and pend["key"] == "tier"
    disputed = [e for e in rt.repo.timeline(m.id) if e["type"] == "BeliefDisputed"]
    assert disputed and disputed[0]["payload"]["conflict"] is True     # CRM vs billing surfaced, not trusted

    rt.approve(m.id, pend["node_id"], "resolve", edit={"value": "gold"})
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    world = rt._world(m.id).snapshot()
    assert world["tier"].value == "gold" and world["tier"].confidence >= 0.75  # authoritative resolution won
    assert world["offer"].value["offer"] == "deal for gold"            # downstream used the resolved value


def test_low_confidence_single_source_gates():
    reg = CapabilityRegistry()
    reg.register(_spec("crm.pull", "crm", ["signal_a"]))
    reg.register(_spec("offers.make", "offers", ["offer"]))
    client = InMemoryOperatorClient({
        "crm.pull": lambda i: {"_observations": [{"key": "tier", "value": "gold",
                                                   "source": "crm", "confidence": 0.4}]},
        "offers.make": lambda i: {"offer": i.get("tier")},
    })
    steps = lambda: [
        IntentStep(outcome="signal_a", need="pull the CRM signal"),
        IntentStep(outcome="offer", need="make an offer", inputs_from=["signal_a", "tier"]),
    ]
    rt = MissionRuntime(reg, Executor(client), planner=_Planner(steps))
    m = rt.create_mission("offer", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN and rt.repo.pending_human(m.id)["key"] == "tier"
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "resolve", edit={"value": "platinum"})
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
