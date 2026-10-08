"""Decision producer for control-tower (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target control-tower's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("bi.ask", "bi.summary", "bi.refresh")


@dataclass(frozen=True)
class AnalyticsSignals:
    metric: str = 'revenue'
    anomaly_detected: bool = False
    question: str = ''


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="control-tower", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"control-tower:{subject}:{kind}")


def control_tower_opportunity(s: AnalyticsSignals) -> DecisionOpportunity:
    subject = s.metric
    a = []
    if s.anomaly_detected or bool(s.question):
        a.append(_c(subject, 'ask', 'Investigate the metric with a governed question', 'bi.ask', RiskTier.READ))
    if True:
        a.append(_c(subject, 'summary', 'Summarise current business performance', 'bi.summary', RiskTier.READ))
    if s.anomaly_detected:
        a.append(_c(subject, 'refresh', 'Refresh the dashboards', 'bi.refresh', RiskTier.READ))
    return DecisionOpportunity(
        entity=subject, source_app="control-tower", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"control-tower:{subject}")


__all__ = ["AnalyticsSignals", "EMITTED_CAPABILITIES", "control_tower_opportunity"]
