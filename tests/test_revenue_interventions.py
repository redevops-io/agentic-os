"""Leakage → governed intervention queue (Revenue & Execution plan §6, §17, §19). Offline.

Each RevenueLeakage becomes a Priority-Engine decision; consequential recovery work parks on approval, weak
signals abstain, and the queue is ranked by priority (highest first).
"""
from __future__ import annotations

from agentic_os.priority_engine import Action, PriorityPolicy
from agentic_os.revenue.interventions import (
    decision_view, intervention_queue, plan_leakage_interventions,
)
from agentic_os.revenue.leakage import LeakageType, RevenueLeakage


def _leak(subject, ev, conf, urg=0.5, amt=1_000_000, lt=LeakageType.STALLED_OPPORTUNITY):
    return RevenueLeakage(lt, subject=subject, expected_value=ev, confidence=conf, urgency=urg,
                          proposed_action=f"Recover {subject}", required_capability="crm.opportunity.followup",
                          amount_cents=amt, observation_refs=(f"ref:{subject}",))


def test_leakage_parks_on_approval_and_ranks_by_priority():
    leaks = [_leak("small", ev=0.2, conf=0.9), _leak("big", ev=0.9, conf=0.95, urg=0.9)]
    decisions = plan_leakage_interventions(leaks)
    assert [d.candidate.subject for d in decisions] == ["big", "small"]        # ranked, highest first
    assert all(d.action is Action.REQUEST_APPROVAL and d.requires_approval for d in decisions)  # consequential


def test_weak_signal_abstains_and_is_dropped():
    # confidence below the policy threshold → ABSTAIN → not in the actionable queue
    leaks = [_leak("weak", ev=0.8, conf=0.2)]
    policy = PriorityPolicy(min_confidence=0.5)
    assert plan_leakage_interventions(leaks, policy=policy) == []
    kept = plan_leakage_interventions(leaks, policy=policy, include_abstained=True)
    assert len(kept) == 1 and kept[0].action is Action.ABSTAIN


def test_intervention_queue_is_serializable_and_summarized():
    leaks = [_leak("acme", ev=0.7, conf=0.9), _leak("globex", ev=0.5, conf=0.9)]
    q = intervention_queue(leaks)
    assert q["count"] == 2 and q["requires_approval"] == 2 and q["auto"] == 0
    top = q["decisions"][0]
    assert top["subject"] == "acme" and top["action"] == "request_approval"
    assert top["required_capabilities"] == ["crm.opportunity.followup"]
    assert top["risk_tier"] == "CONSEQUENTIAL" and top["observation_refs"] == ["ref:acme"]


def test_decision_view_shape():
    d = plan_leakage_interventions([_leak("x", ev=0.6, conf=0.9)])[0]
    v = decision_view(d)
    assert set(v) >= {"subject", "action", "requires_approval", "priority", "proposed_action",
                      "required_capabilities", "risk_tier", "rationale", "candidate_id"}
