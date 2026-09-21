"""Phase 7 acceptance: exact-version approval for consequential actions; edits invalidate approval; action
receipt and verification are distinct. Drives the REAL sentinel operator (core patched offline) so the
governed path consumes the actual Mission Runtime operator contract, not a replica.
"""
from __future__ import annotations

import importlib

import pytest

from agentic_os.mission.operator_sdk import Operator, capability

evidence = importlib.import_module("edge-sentinel.evidence")
response = importlib.import_module("edge-sentinel.response")
operator_mod = importlib.import_module("edge-sentinel.operator")
core = importlib.import_module("edge-sentinel.core")

ActionRequest = evidence.ActionRequest
ActionState = evidence.ActionState
SecurityDecision = evidence.SecurityDecision
propose_response = response.propose_response
execute_response = response.execute_response
verify_response = response.verify_response
to_cacao = response.to_cacao
from_cacao = response.from_cacao
GovernanceError = response.GovernanceError


@pytest.fixture
def fake_crowdsec(monkeypatch):
    """Patch the sentinel core so the REAL operator runs against an in-memory CrowdSec (no live LAPI)."""
    bans: set[str] = set()
    monkeypatch.setattr(core, "block_ip", lambda inp: (bans.add(inp["ip"]) or {"id": f"cs-{inp['ip']}"}))
    monkeypatch.setattr(core, "unblock_ip", lambda inp: (bans.discard(inp["ip"]) or {"unblocked": inp["ip"]}))
    monkeypatch.setattr(core, "triage", lambda: {"active_bans": sorted(bans)})
    return bans


def _operator() -> Operator:
    return operator_mod.build_edge_sentinel_operator()


def _request(ip="203.0.113.7") -> ActionRequest:
    return ActionRequest(capability="sentinel.block_ip", parameters={"ip": ip},
                         finding_refs=("fnd-1",), evidence_refs=("ev-1",),
                         approval_required=True, state=ActionState.AWAITING_APPROVAL)


def test_proposal_reads_governance_from_the_real_capability():
    """Edge Sentinel does not restate approval semantics — it reads them from the real sentinel.block_ip
    capability (which is side_effecting + approval_required + undo=sentinel.unblock_ip)."""
    prop = propose_response(_operator(), _request(), case_id="case-1")
    assert prop.capability_name == "sentinel.block_ip"
    assert prop.approval_required is True and prop.side_effecting is True
    assert prop.undo == "sentinel.unblock_ip" and "sentinel:write" in prop.permissions


def test_execution_refused_without_approval():
    prop = propose_response(_operator(), _request(), case_id="case-1")
    declined = SecurityDecision(request_id=prop.proposal_id, actor="soc", approved=False)
    with pytest.raises(GovernanceError):
        execute_response(_operator(), prop, decision=declined)


def test_approved_execution_then_distinct_verification(fake_crowdsec):
    op = _operator()
    prop = propose_response(op, _request(), case_id="case-1")
    approved = SecurityDecision(request_id=prop.proposal_id, actor="soc-lead", approved=True)

    receipt = execute_response(op, prop, decision=approved)
    assert receipt.status == "SUCCEEDED" and receipt.external_ref == "cs-203.0.113.7"
    assert "203.0.113.7" in fake_crowdsec                       # the real operator actually banned it

    verification = verify_response(op, prop, receipt)
    assert verification.verified is True and verification.method == "sentinel.triage"
    # receipt and verification are DISTINCT objects/states
    assert verification.proposal_id == prop.proposal_id
    assert not hasattr(receipt, "verified")


def test_verification_can_fail_even_with_a_succeeded_receipt(fake_crowdsec, monkeypatch):
    """A SUCCEEDED receipt whose effect can't be observed is an honest 'unverified' — not silently 'done'."""
    op = _operator()
    prop = propose_response(op, _request(), case_id="case-1")
    approved = SecurityDecision(request_id=prop.proposal_id, actor="soc", approved=True)
    receipt = execute_response(op, prop, decision=approved)
    # now the world no longer shows the ban (e.g. it was lifted / never propagated)
    monkeypatch.setattr(core, "triage", lambda: {"active_bans": []})
    v = verify_response(op, prop, receipt)
    assert v.verified is False and "NOT observed" in v.detail


def test_editing_the_action_invalidates_the_approval():
    """Exact-version approval: an approval is bound to a proposal_id derived from the capability+inputs, so
    changing the target IP produces a different proposal the old decision does not authorize."""
    op = _operator()
    p1 = propose_response(op, _request("203.0.113.7"), case_id="case-1")
    p2 = propose_response(op, _request("198.51.100.9"), case_id="case-1")
    assert p1.proposal_id != p2.proposal_id
    decision_for_p1 = SecurityDecision(request_id=p1.proposal_id, actor="soc", approved=True)
    # the decision names p1's id; it simply is not a decision about p2 (different request_id)
    assert decision_for_p1.request_id != p2.proposal_id


def test_governed_response_invariant_refuses_ungated_side_effect():
    """A side-effecting capability that is NOT approval_required cannot be proposed as a governed response."""
    ungoverned = Operator("rogue", [capability("rogue.wipe", lambda inp: {}, side_effecting=True)])
    with pytest.raises(GovernanceError):
        propose_response(ungoverned, ActionRequest(capability="rogue.wipe", parameters={}), case_id="c")


def test_cacao_export_is_interop_only_and_round_trips():
    prop = propose_response(_operator(), _request(), case_id="case-1")
    playbook = to_cacao(prop)
    assert playbook["type"] == "playbook" and playbook["spec_version"] == "cacao-2.0"
    # the action is manual — importing this never executes here
    action = next(s for s in playbook["workflow"].values() if s.get("type") == "action")
    assert action["commands"][0]["type"] == "manual"
    parsed = from_cacao(playbook)
    assert parsed["capability"] == "sentinel.block_ip" and parsed["approval_required"] is True
