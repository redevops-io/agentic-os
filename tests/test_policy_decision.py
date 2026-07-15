"""P8 — the Policy & Human-Decision plane.

Approval is a decision, not a static flag: mandatory-capability OR mission-policy OR a dynamic
risk×value assessment (reversibility, uncertainty, monetary value, permissions, regulatory
sensitivity, novelty, blast radius, verification coverage). When it fires, the approver gets an
evidence-backed packet. `capability.approval_required` is kept as one input, not removed.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
)


def _fleet(spec_kwargs, *, handler=None, extra=None):
    reg = CapabilityRegistry()
    specs = [CapabilitySpec("op.act", "op", provides=["done"], **spec_kwargs)]
    if extra:
        specs += extra
    reg.register(CapabilityManifest("op", specs))
    client = InMemoryOperatorClient({"op.act": handler or (lambda i: {"ok": True})})
    return reg, client


class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id,
                               steps=[IntentStep(outcome="done", need="do the thing")])


def _parked(rt, mid) -> dict:
    parks = [e["payload"] for e in rt.repo.timeline(mid) if e["type"] == "NodeParked"]
    return parks[-1] if parks else {}


# ─── dynamic risk gates a risky action that carries NO static flag ────────────
def test_risky_action_gated_by_dynamic_risk():
    # irreversible (no undo) AND performed by a low-confidence / unproven capability — the
    # combination, not irreversibility alone, crosses the threshold. No approval_required flag.
    reg, client = _fleet(dict(side_effecting=True, confidence=0.3))
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    assert rt.repo.state(m.id) == MissionState.WAITING_HUMAN
    park = _parked(rt, m.id)
    assert "dynamic_risk" in park["rules"]
    assert park["risk"]["reversibility"] == 0.0 and park["risk"]["score"] >= 0.5
    # …and a human can clear it → it runs to completion
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert rt.repo.state(m.id) == MissionState.SUCCEEDED


# ─── a reversible, low-value, verified action is NOT gated ────────────────────
def test_reversible_low_risk_runs_without_a_gate():
    reg, client = _fleet(dict(side_effecting=True, undo="op.undo"))
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.SUCCEEDED
    assert not [e for e in rt.repo.timeline(m.id) if e["type"] == "NodeParked"]


# ─── regulatory sensitivity floors risk high even for a reversible action ─────
def test_regulatory_capability_is_gated():
    reg, client = _fleet(dict(side_effecting=True, undo="op.undo", permissions=["pii:write"]))
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["pii:write"])   # granted, else compile fails closed
    rt.run(m.id)
    assert rt.repo.state(m.id) == MissionState.WAITING_HUMAN
    park = _parked(rt, m.id)
    assert park["risk"]["regulatory"] is True and "dynamic_risk" in park["rules"]


# ─── the mission's own policy can demand approval for anything ─────────────────
def test_mission_policy_forces_a_gate():
    reg, client = _fleet(dict())                              # trivial, not even side-effecting
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"], constraints=["approval:all"])
    rt.run(m.id)
    assert rt.repo.state(m.id) == MissionState.WAITING_HUMAN
    assert "mission_policy" in _parked(rt, m.id)["rules"]


# ─── a statically-mandatory capability still gates (input kept, not removed) ───
def test_mandatory_capability_still_gates():
    reg, client = _fleet(dict(approval_required=True))
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    assert rt.repo.state(m.id) == MissionState.WAITING_HUMAN
    assert "mandatory_capability" in _parked(rt, m.id)["rules"]


# ─── the approver gets an evidence-backed packet, not just a yes/no ───────────
def test_approval_packet_is_evidence_backed():
    # op2 also provides "done" but needs a grant this mission lacks → it's scoped out of the plan
    # (so op.act is the bound node) yet still surfaces as a known alternative in the packet.
    alt = CapabilitySpec("op2.act", "op2", provides=["done"], side_effecting=True,
                         undo="op2.undo", permissions=["special:write"])
    reg, client = _fleet(dict(side_effecting=True, confidence=0.3), extra=[alt])
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("act", policy_refs=[])            # grants op.act (no perms), not op2
    rt.run(m.id)
    pkt = _parked(rt, m.id)["approval_packet"]
    assert pkt["proposed_action"]["capability"] == "op.act"
    assert pkt["rollback"].startswith("IRREVERSIBLE")
    assert pkt["risk"]["reversibility"] == 0.0
    assert "op2.act" in pkt["alternatives"]                  # a reversible alternative is surfaced
    assert set(pkt) >= {"expected_impact", "supporting_evidence", "unresolved_claims", "confidence"}
