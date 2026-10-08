"""Decision producer for market-radar (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target market-radar's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("radar.brief", "radar.add_watch")


@dataclass(frozen=True)
class RadarSignals:
    subject: str = 'competitor'
    change_detected: bool = False
    new_competitor: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="market-radar", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"market-radar:{subject}:{kind}")


def market_radar_opportunity(s: RadarSignals) -> DecisionOpportunity:
    subject = s.subject
    a = []
    if s.change_detected:
        a.append(_c(subject, 'brief', 'Brief on the detected competitor change', 'radar.brief', RiskTier.READ))
    if s.new_competitor:
        a.append(_c(subject, 'add_watch', 'Add a watch for the new competitor', 'radar.add_watch', RiskTier.BOUNDED_WRITE))
    return DecisionOpportunity(
        entity=subject, source_app="market-radar", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"market-radar:{subject}")


__all__ = ["RadarSignals", "EMITTED_CAPABILITIES", "market_radar_opportunity"]
