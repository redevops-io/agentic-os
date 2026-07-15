"""P7 — the Evidence + Verification plane.

The invariant under test: *no consequential operator result becomes authoritative World State
until the applicable acceptance policy succeeds.* A malformed/permission-losing result is REJECTed
(hard fail); an unmet assertion ESCALATEs to a human who accepts (commit) or rejects (fail); a clean
result is ACCEPTed and the whole flow is recorded in an append-only evidence ledger.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.verify import CompositeVerifier
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
    Node, StateAssertion, VerificationDecision, VerificationResult,
)


def _receipt_fleet(handler):
    """One side-effecting capability that declares a 'receipt' output — the verifier's syntactic
    check requires the result to carry it."""
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec(
        "op.act", "op", provides=["done"], side_effecting=True,
        outputs={"receipt": "string"}, permissions=[])]))
    return reg, InMemoryOperatorClient({"op.act": handler})


class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id,
                               steps=[IntentStep(outcome="done", need="do the thing")])


class _Escalate:
    """A verifier that forces ESCALATE on one capability — exercises the human-in-the-loop gate
    without needing the compiler to attach assertions."""
    def __init__(self, cap): self.cap = cap

    def verify(self, node, result, world, mission) -> VerificationResult:
        d = VerificationDecision.ESCALATE if node.capability == self.cap else VerificationDecision.ACCEPT
        return VerificationResult(decision=d, checks=[
            {"stage": "assertion", "passed": d is not VerificationDecision.ESCALATE, "detail": "needs review"}])


# ─── ACCEPT: a clean consequential result commits and is recorded ─────────────
def test_valid_result_is_accepted_and_recorded():
    reg, client = _receipt_fleet(lambda i: {"receipt": "r1"})
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.SUCCEEDED
    ex = rt.explain(m.id)
    recorded = [r for r in ex["evidence"] if r["type"] == "VerificationRecorded"]
    assert recorded and recorded[0]["decision"] == "accept"           # ledger is append-only + audit-grade
    assert ex["learning"]["verification"]["op.act"] == 1.0            # verification-policy loop learned it passes


# ─── REJECT: a malformed result never becomes authoritative state (hard fail) ─
def test_malformed_result_is_rejected_hard():
    reg, client = _receipt_fleet(lambda i: {"ok": True})               # missing declared 'receipt'
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.FAILED
    vf = [e for e in rt.repo.timeline(m.id) if e["type"] == "VerificationFailed"]
    assert vf and vf[0]["payload"]["decision"] == "reject"
    assert rt.explain(m.id)["learning"]["verification"]["op.act"] == 0.0


# ─── ESCALATE → human accept commits the parked result ───────────────────────
def test_escalation_parks_then_human_accept_commits():
    reg, client = _receipt_fleet(lambda i: {"receipt": "r1"})
    rt = MissionRuntime(reg, Executor(client), planner=_P(), verifier=_Escalate("op.act"))
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    assert rt.repo.state(m.id) == MissionState.WAITING_HUMAN
    pend = rt.repo.pending_human(m.id)
    assert pend["kind"] == "verification" and pend["options"] == ["accept", "reject"]

    rt.approve(m.id, pend["node_id"], "accept")                        # override → result is committed
    assert rt.repo.state(m.id) == MissionState.SUCCEEDED
    tl = rt.repo.timeline(m.id)
    assert any(e["type"] == "VerificationOverridden" for e in tl)


# ─── ESCALATE → human reject fails the mission ───────────────────────────────
def test_escalation_human_reject_fails():
    reg, client = _receipt_fleet(lambda i: {"receipt": "r1"})
    rt = MissionRuntime(reg, Executor(client), planner=_P(), verifier=_Escalate("op.act"))
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    node_id = rt.repo.pending_human(m.id)["node_id"]
    rt.approve(m.id, node_id, "reject")
    assert rt.repo.state(m.id) == MissionState.FAILED
    assert any(e["type"] == "VerificationRejected" for e in rt.repo.timeline(m.id))


# ─── the CompositeVerifier's state-transition stage (unit) ───────────────────
def test_composite_verifier_assertions():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec("op.act", "op", permissions=[])]))
    v = CompositeVerifier(reg, lambda m: {"*"})
    node = Node(capability="op.act", operator="op", side_effecting=True,
                assertions=[StateAssertion(key="approved", op="truthy")])
    assert v.verify(node, {"approved": False}, None, None).decision is VerificationDecision.ESCALATE
    assert v.verify(node, {"approved": True}, None, None).decision is VerificationDecision.ACCEPT


def test_composite_verifier_lost_permission_rejects():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec(
        "op.act", "op", side_effecting=True, permissions=["billing:write"])]))
    v = CompositeVerifier(reg, lambda m: set())                        # grant revoked mid-flight
    node = Node(capability="op.act", operator="op", side_effecting=True)
    assert v.verify(node, {}, None, None).decision is VerificationDecision.REJECT
