"""Decision producer for guide (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target guide's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("guide.retrieve", "guide.walkthrough")


@dataclass(frozen=True)
class GuideSignals:
    user: str = 'user'
    question: str = ''
    task: str = ''


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="guide", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"guide:{subject}:{kind}")


def guide_opportunity(s: GuideSignals) -> DecisionOpportunity:
    subject = s.user
    a = []
    if bool(s.question):
        a.append(_c(subject, 'retrieve', 'Retrieve the relevant help', 'guide.retrieve', RiskTier.READ))
    if bool(s.task):
        a.append(_c(subject, 'walkthrough', 'Offer a task walkthrough', 'guide.walkthrough', RiskTier.READ))
    return DecisionOpportunity(
        entity=subject, source_app="guide", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"guide:{subject}")


__all__ = ["GuideSignals", "EMITTED_CAPABILITIES", "guide_opportunity"]
