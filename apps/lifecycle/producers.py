"""Decision producer for lifecycle (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target lifecycle's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("lifecycle.segment", "lifecycle.compose_campaign", "lifecycle.suggest_flow")


@dataclass(frozen=True)
class LifecycleSignals:
    audience: str = 'all'
    segment_drift: bool = False
    campaign_due: bool = False
    flow_gap: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="lifecycle", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"lifecycle:{subject}:{kind}")


def lifecycle_opportunity(s: LifecycleSignals) -> DecisionOpportunity:
    subject = s.audience
    a = []
    if s.segment_drift:
        a.append(_c(subject, 'segment', 'Re-segment the audience', 'lifecycle.segment', RiskTier.READ))
    if s.campaign_due:
        a.append(_c(subject, 'compose_campaign', 'Compose the due lifecycle campaign', 'lifecycle.compose_campaign', RiskTier.BOUNDED_WRITE))
    if s.flow_gap:
        a.append(_c(subject, 'suggest_flow', 'Suggest an automation flow', 'lifecycle.suggest_flow', RiskTier.READ))
    return DecisionOpportunity(
        entity=subject, source_app="lifecycle", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"lifecycle:{subject}")


__all__ = ["LifecycleSignals", "EMITTED_CAPABILITIES", "lifecycle_opportunity"]
