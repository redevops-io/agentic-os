"""Deal Closing Intelligence Phase 8 — bounded automation.

Proves the most conservative module denies by default and only auto-runs a NARROW, reversible, allow-listed set
within caps: pricing/contracts/external comms are never automated; automation applies only at POLICY_AUTHORIZED
and never escalates an action the autonomy level already gated; per-deal and global caps are enforced; a denied
action consumes no slot.
"""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.deal_closing import (
    AutomationBounds, AutomationGovernor, AutonomyLevel, DEFAULT_ALLOWED, InterventionKind,
)
from agentic_os.deal_closing.candidates import CandidateAction, no_action


def _action(kind, deal_ref="d1", reversibility=1.0):
    return CandidateAction(kind=kind, deal_ref=deal_ref, target_condition="X", p_resolves=0.6,
                           reversibility=reversibility)


def test_default_allow_list_excludes_high_downside():
    assert InterventionKind.VERIFY_CLAIM in DEFAULT_ALLOWED
    for forbidden in (InterventionKind.AGREE_PRICING, InterventionKind.SEND_CONTRACT,
                      InterventionKind.ADVANCE_SIGNATURE, InterventionKind.ENGAGE_ECONOMIC_BUYER,
                      InterventionKind.FOLLOW_UP_QUOTE):
        assert forbidden not in DEFAULT_ALLOWED


def test_pricing_never_automated_even_at_policy_authorized():
    gov = AutomationGovernor()
    d = gov.authorize(_action(InterventionKind.AGREE_PRICING), autonomy=AutonomyLevel.POLICY_AUTHORIZED)
    assert d.allowed is False


def test_allowed_kind_auto_runs_at_policy_authorized():
    gov = AutomationGovernor()
    d = gov.authorize(_action(InterventionKind.VERIFY_CLAIM), autonomy=AutonomyLevel.POLICY_AUTHORIZED)
    assert d.allowed is True
    assert gov.counts()[0] == 1


def test_no_automation_below_policy_authorized():
    gov = AutomationGovernor()
    d = gov.authorize(_action(InterventionKind.VERIFY_CLAIM), autonomy=AutonomyLevel.APPROVAL_GATED)
    assert d.allowed is False
    assert gov.counts()[0] == 0        # denied → no slot consumed


def test_per_deal_cap_enforced_and_denial_consumes_no_slot():
    gov = AutomationGovernor(AutomationBounds(per_deal_cap=1))
    a = _action(InterventionKind.VERIFY_CLAIM, deal_ref="d1")
    b = _action(InterventionKind.MAP_DECISION_PROCESS, deal_ref="d1")
    assert gov.authorize(a, autonomy=AutonomyLevel.POLICY_AUTHORIZED).allowed is True
    second = gov.authorize(b, autonomy=AutonomyLevel.POLICY_AUTHORIZED)
    assert second.allowed is False      # per-deal cap reached
    glob, per = gov.counts()
    assert glob == 1 and per["d1"] == 1


def test_global_cap_enforced():
    gov = AutomationGovernor(AutomationBounds(per_deal_cap=99, global_cap=2))
    for i in range(2):
        assert gov.authorize(_action(InterventionKind.VERIFY_CLAIM, deal_ref=f"d{i}"),
                             autonomy=AutonomyLevel.POLICY_AUTHORIZED).allowed is True
    assert gov.authorize(_action(InterventionKind.VERIFY_CLAIM, deal_ref="d9"),
                         autonomy=AutonomyLevel.POLICY_AUTHORIZED).allowed is False


def test_irreversible_action_denied():
    # an allow-listed kind that is nonetheless not reversible enough is still denied
    gov = AutomationGovernor(AutomationBounds(min_reversibility=0.95))
    d = gov.authorize(_action(InterventionKind.MAP_DECISION_PROCESS, reversibility=0.5),
                      autonomy=AutonomyLevel.POLICY_AUTHORIZED)
    assert d.allowed is False


def test_no_action_not_automated():
    gov = AutomationGovernor()
    assert gov.authorize(no_action("d1"), autonomy=AutonomyLevel.POLICY_AUTHORIZED).allowed is False
