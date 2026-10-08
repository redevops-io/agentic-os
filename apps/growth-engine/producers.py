"""Decision producer for growth-engine (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target growth-engine's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("growth.analyze", "growth.reallocate_budget")


@dataclass(frozen=True)
class GrowthSignals:
    site: str = 'redevops.io'
    channel_underperforming: bool = False
    spend_anomaly: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="growth-engine", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"growth-engine:{subject}:{kind}")


def growth_opportunity(s: GrowthSignals) -> DecisionOpportunity:
    subject = s.site
    a = []
    if True:
        a.append(_c(subject, 'analyze', 'Analyse channel attribution', 'growth.analyze', RiskTier.READ))
    if s.channel_underperforming or s.spend_anomaly:
        a.append(_c(subject, 'reallocate_budget', 'Reallocate the channel budget', 'growth.reallocate_budget', RiskTier.CONSEQUENTIAL))
    return DecisionOpportunity(
        entity=subject, source_app="growth-engine", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"growth-engine:{subject}")


__all__ = ["GrowthSignals", "EMITTED_CAPABILITIES", "growth_opportunity"]
