"""Decision producer for agentic-privacy (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target agentic-privacy's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("privacy.intake", "privacy.access", "privacy.delete", "privacy.retention")


@dataclass(frozen=True)
class PrivacySignals:
    request_id: str = 'r-1'
    new_request: bool = False
    access_request: bool = False
    erasure_request: bool = False
    retention_due: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="agentic-privacy", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"agentic-privacy:{subject}:{kind}")


def privacy_opportunity(s: PrivacySignals) -> DecisionOpportunity:
    subject = s.request_id
    a = []
    if s.new_request:
        a.append(_c(subject, 'intake', 'Intake and verify the data-subject request', 'privacy.intake', RiskTier.BOUNDED_WRITE))
    if s.access_request:
        a.append(_c(subject, 'access', 'Fulfil the data-access request', 'privacy.access', RiskTier.READ))
    if s.erasure_request:
        a.append(_c(subject, 'delete', 'Execute the verified erasure', 'privacy.delete', RiskTier.CONSEQUENTIAL))
    if s.retention_due:
        a.append(_c(subject, 'retention', 'Apply the retention policy', 'privacy.retention', RiskTier.READ))
    return DecisionOpportunity(
        entity=subject, source_app="agentic-privacy", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"agentic-privacy:{subject}")


__all__ = ["PrivacySignals", "EMITTED_CAPABILITIES", "privacy_opportunity"]
