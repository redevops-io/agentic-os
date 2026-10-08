"""Decision -> Mission bridge: records always written; actionable selections author a mission."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import ApprovalPolicy, RiskTier
from agentic_os.app_kit import (
    BridgeError,
    BridgeOutcome,
    MissionRequest,
    bridge_selection,
    build_request,
)
from agentic_os.priority_engine import (
    Action,
    DecisionOpportunity,
    InterventionCandidate,
    select_action,
)


class FakeStore:
    def __init__(self):
        self.records = []

    def record(self, r):
        self.records.append(r)


class FakeLauncher:
    def __init__(self, mission_id="m-123"):
        self.mission_id = mission_id
        self.requests = []

    def launch(self, request: MissionRequest) -> str:
        self.requests.append(request)
        return self.mission_id


def _opp(candidate: InterventionCandidate, *, opp_id="opp-1"):
    return DecisionOpportunity(
        entity=candidate.subject,
        source_app=candidate.source_app,
        candidate_actions=(candidate,),
        opportunity_id=opp_id,
    )


def _select(candidate):
    return select_action(_opp(candidate))


# ── ACT: authors an ungated mission, records the mission_id ──────────────────

def test_act_launches_ungated():
    cand = InterventionCandidate(
        source_app="agentic-crm", subject="ACME", proposed_action="send proposal",
        expected_value=8.0, confidence=0.95, risk_tier=RiskTier.READ,
        approval_policy=ApprovalPolicy.AUTO, required_capabilities=("crm.send_proposal",),
        candidate_id="c-act", action_kind="send_proposal",
    )
    sel = _select(cand)
    assert sel.decision.action is Action.ACT

    store, launcher = FakeStore(), FakeLauncher()
    res = bridge_selection(sel, store=store, policy_version="assist-1", launcher=launcher)

    assert res.outcome is BridgeOutcome.LAUNCHED
    assert res.mission_id == "m-123"
    assert res.request.capability == "crm.send_proposal"
    assert res.request.gated is False
    assert res.request.intervention_id == res.record.intervention_id
    # the record links back to the mission and the decision
    assert store.records == [res.record]
    assert res.record.mission_id == "m-123"
    assert launcher.requests[0].action_kind == "send_proposal"


# ── REQUEST_APPROVAL: authors a GATED mission ────────────────────────────────

def test_request_approval_launches_gated():
    cand = InterventionCandidate(
        source_app="billing", subject="INV-9", proposed_action="issue refund",
        expected_value=10.0, confidence=0.9, risk_tier=RiskTier.CONSEQUENTIAL,
        approval_policy=ApprovalPolicy.REQUIRED, required_capabilities=("billing.refund",),
        candidate_id="c-refund", action_kind="refund",
    )
    sel = _select(cand)
    assert sel.decision.action is Action.REQUEST_APPROVAL

    store, launcher = FakeStore(), FakeLauncher("m-refund")
    res = bridge_selection(sel, store=store, policy_version="assist-1", launcher=launcher)

    assert res.outcome is BridgeOutcome.LAUNCHED_GATED
    assert res.request.gated is True
    assert res.mission_id == "m-refund"


# ── ABSTAIN / do-nothing: record written, nothing launched ───────────────────

def test_low_confidence_abstains_without_launch():
    cand = InterventionCandidate(
        source_app="market-radar", subject="competitor", proposed_action="rush a response",
        expected_value=3.0, confidence=0.20,  # below min_confidence -> abstain / loses to do-nothing
        required_capabilities=("radar.brief",), candidate_id="c-weak", action_kind="brief",
    )
    sel = _select(cand)
    store, launcher = FakeStore(), FakeLauncher()
    res = bridge_selection(sel, store=store, policy_version="assist-1", launcher=launcher)

    assert res.outcome is BridgeOutcome.ABSTAINED
    assert res.mission_id == ""
    assert res.request is None
    assert launcher.requests == []          # nothing launched
    assert len(store.records) == 1          # but the decision IS recorded (N1)


# ── plan-only (no launcher): record written, request returned, empty mission_id ──

def test_plan_only_without_launcher():
    cand = InterventionCandidate(
        source_app="agentic-crm", subject="ACME", proposed_action="send proposal",
        expected_value=8.0, confidence=0.95, risk_tier=RiskTier.READ,
        approval_policy=ApprovalPolicy.AUTO, required_capabilities=("crm.send_proposal",),
        candidate_id="c-act", action_kind="send_proposal",
    )
    sel = _select(cand)
    store = FakeStore()
    res = bridge_selection(sel, store=store, policy_version="assist-1")  # no launcher
    assert res.outcome is BridgeOutcome.LAUNCHED
    assert res.mission_id == ""
    assert res.request is not None
    assert store.records[0].mission_id == ""


# ── phantom actionable candidate (no capability) is rejected ─────────────────

def test_actionable_without_capability_raises():
    cand = InterventionCandidate(
        source_app="x", subject="y", proposed_action="do a thing",
        expected_value=8.0, confidence=0.95, risk_tier=RiskTier.READ,
        approval_policy=ApprovalPolicy.AUTO, required_capabilities=(),  # <- phantom
        candidate_id="c-nocap", action_kind="thing",
    )
    sel = _select(cand)
    assert sel.decision.action is Action.ACT
    with pytest.raises(BridgeError, match="no required_capabilities"):
        build_request(sel, intervention_id="i-1")


def test_intervention_id_is_stable_across_record_and_request():
    cand = InterventionCandidate(
        source_app="a", subject="s", proposed_action="act", expected_value=8.0, confidence=0.95,
        risk_tier=RiskTier.READ, approval_policy=ApprovalPolicy.AUTO,
        required_capabilities=("a.do",), candidate_id="c", action_kind="do",
    )
    sel = _select(cand)
    res = bridge_selection(sel, store=FakeStore(), policy_version="v", launcher=FakeLauncher(),
                           intervention_id="fixed-id")
    assert res.record.intervention_id == "fixed-id"
    assert res.request.intervention_id == "fixed-id"
