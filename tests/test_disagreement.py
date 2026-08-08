"""Disagreement & Decision Evidence (spec §6 acceptance bar).

When independent readers disagree on a MATERIAL field, the runtime does not silently choose
one — it routes the disagreement to the controller. These tests pin the six guarantees:

  1. neither the deterministic nor the model reader is privileged
  2. disagreement on a material field blocks execution
  3. agreement allows execution
  4. cosmetic disagreement does not block
  5. evidence survives replay
  6. removing the disagreement consumer (the material gate) regresses a named test

Comprehensive tests come next; this is the spec's minimum bar.
"""
from __future__ import annotations

from agentic_os.mission import belief
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore, WorldState
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, DecisionEvidence, ExecutionIntent, IntentStep,
    MissionState,
)


def _spec(name, op, provides, **kw):
    return CapabilityManifest(op, [CapabilitySpec(name, op, provides=provides, **kw)])


class _Planner:
    def __init__(self, steps): self._steps = steps
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=self._steps())


# ── 1. neither deterministic nor model reader is privileged ──────────────────
def test_no_reader_type_is_privileged():
    """Two readers, equal confidence, different values, different source_types → conflict.
    The winner is decided by confidence mass + recency, never by source_type. Swapping which
    reader is 'regex' vs 'model' must not change the outcome."""
    a = belief.fuse([
        {"key": "amount", "value": 100, "source": "r", "source_type": "regex", "confidence": 1.0, "ts": 1.0},
        {"key": "amount", "value": 200, "source": "m", "source_type": "model", "confidence": 1.0, "ts": 1.0},
    ], key="amount")
    b = belief.fuse([
        {"key": "amount", "value": 100, "source": "r", "source_type": "model", "confidence": 1.0, "ts": 1.0},
        {"key": "amount", "value": 200, "source": "m", "source_type": "regex", "confidence": 1.0, "ts": 1.0},
    ], key="amount")
    assert a.conflict and b.conflict                 # genuine disagreement surfaced both ways
    assert a.value == b.value                         # swapping source_types did NOT change the winner
    # and both readers' evidence is preserved, typed, un-ranked by source_type
    assert {e.source_type for e in a.evidence} == {"regex", "model"}
    assert {e.value for e in a.evidence} == {100, 200}


# ── 2. disagreement on a MATERIAL field blocks execution ─────────────────────
def _two_reader_mission(cosmetic: bool):
    reg = CapabilityRegistry()
    reg.register(_spec("regex.read", "regexer", ["r_read"]))
    reg.register(_spec("model.read", "modeler", ["m_read"]))
    reg.register(_spec("pay.invoice", "pay", ["paid"], side_effecting=True))
    client = InMemoryOperatorClient({
        "regex.read": lambda i: {"_observations": [
            {"key": "amount", "value": 100, "source": "regexer", "source_type": "regex", "source_ref": "rule:total"}]},
        "model.read": lambda i: {"_observations": [
            {"key": "amount", "value": 200, "source": "modeler", "source_type": "model", "source_ref": "span:12-18"}]},
        "pay.invoice": lambda i: {"paid": i.get("amount")},
    })
    pay_step = IntentStep(outcome="paid", need="pay the invoice", inputs_from=["r_read", "m_read", "amount"],
                          cosmetic_inputs=["amount"] if cosmetic else [])
    steps = lambda: [
        IntentStep(outcome="r_read", need="regex read the amount"),
        IntentStep(outcome="m_read", need="model read the amount"),
        pay_step,
    ]
    rt = MissionRuntime(reg, Executor(client), planner=_Planner(steps))
    m = rt.create_mission("pay the invoice", policy_refs=["*"])
    rt.run(m.id)
    return rt, m


def test_material_disagreement_blocks_execution():
    rt, m = _two_reader_mission(cosmetic=False)
    assert m.state == MissionState.WAITING_HUMAN                 # did NOT silently pick 100 or 200
    pend = rt.repo.pending_human(m.id)
    assert pend["kind"] == "disambiguation" and pend["key"] == "amount"
    disputed = [e for e in rt.repo.timeline(m.id) if e["type"] == "BeliefDisputed"]
    assert disputed and disputed[0]["payload"]["conflict"] is True


# ── 3. agreement allows execution ────────────────────────────────────────────
def test_agreement_allows_execution():
    reg = CapabilityRegistry()
    reg.register(_spec("regex.read", "regexer", ["r_read"]))
    reg.register(_spec("model.read", "modeler", ["m_read"]))
    reg.register(_spec("pay.invoice", "pay", ["paid"], side_effecting=True))
    client = InMemoryOperatorClient({
        "regex.read": lambda i: {"_observations": [
            {"key": "amount", "value": 100, "source": "regexer", "source_type": "regex"}]},
        "model.read": lambda i: {"_observations": [
            {"key": "amount", "value": 100, "source": "modeler", "source_type": "model"}]},  # agree
        "pay.invoice": lambda i: {"paid": i.get("amount")},
    })
    steps = lambda: [
        IntentStep(outcome="r_read", need="regex read"),
        IntentStep(outcome="m_read", need="model read"),
        IntentStep(outcome="paid", need="pay", inputs_from=["r_read", "m_read", "amount"]),
    ]
    rt = MissionRuntime(reg, Executor(client), planner=_Planner(steps))
    m = rt.create_mission("pay", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.SUCCEEDED
    assert rt._world(m.id).snapshot()["paid"].value["paid"] == 100


# ── 4. cosmetic disagreement does NOT block ──────────────────────────────────
def test_cosmetic_disagreement_does_not_block():
    rt, m = _two_reader_mission(cosmetic=True)                   # same conflict, but 'amount' declared cosmetic
    assert m.state == MissionState.SUCCEEDED                     # non-material disagreement never gates
    assert not [e for e in rt.repo.timeline(m.id) if e["type"] == "BeliefDisputed"]


# ── 5. evidence survives replay ──────────────────────────────────────────────
def test_evidence_survives_replay():
    """Beliefs (and their DecisionEvidence) are recomputed from the ObservationWritten event log,
    so a fresh WorldState over the same store reconstructs identical typed evidence."""
    store = EventStore()
    w = WorldState("mw_1", store, belief.fuse)
    w.observe("amount", 100, "regexer", 1.0, source_type="regex", source_ref="rule:total")
    w.observe("amount", 200, "modeler", 1.0, source_type="model", source_ref="span:12-18")
    # replay: a brand-new WorldState reading the same event store
    replayed = WorldState("mw_1", store, belief.fuse).belief("amount")
    assert replayed is not None and replayed.conflict
    by_type = {e.source_type: e for e in replayed.evidence}
    assert isinstance(by_type["regex"], DecisionEvidence)
    assert by_type["regex"].value == 100 and by_type["regex"].source_ref == "rule:total"
    assert by_type["model"].value == 200 and by_type["model"].source_ref == "span:12-18"
    assert all(e.field == "amount" for e in replayed.evidence)


# ── 6. removing the material gate regresses this named test ──────────────────
def test_removing_the_disagreement_gate_regresses_this():
    """The disagreement *consumer* is the material gate in MissionRuntime._belief_issue /
    _park_disambiguation. If that gate is removed, a material disagreement would flow straight
    into the side-effecting pay node and this assertion (WAITING_HUMAN, not SUCCEEDED) fails —
    i.e. the feature is load-bearing, not decorative (spec §6)."""
    rt, m = _two_reader_mission(cosmetic=False)
    assert m.state == MissionState.WAITING_HUMAN
    assert m.state != MissionState.SUCCEEDED          # the side effect did NOT happen on a disputed amount
