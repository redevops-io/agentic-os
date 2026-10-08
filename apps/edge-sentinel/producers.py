"""Decision producer for edge-sentinel (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target edge-sentinel's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("sentinel.triage", "sentinel.block_ip", "sentinel.unblock_ip")


@dataclass(frozen=True)
class SentinelSignals:
    ip: str = '0.0.0.0'
    new_alert: bool = False
    confirmed_threat: bool = False
    false_positive: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="edge-sentinel", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"edge-sentinel:{subject}:{kind}")


def edge_sentinel_opportunity(s: SentinelSignals) -> DecisionOpportunity:
    subject = s.ip
    a = []
    if s.new_alert:
        a.append(_c(subject, 'triage', 'Triage the new alert', 'sentinel.triage', RiskTier.READ))
    if s.confirmed_threat:
        a.append(_c(subject, 'block_ip', 'Block the confirmed-malicious IP', 'sentinel.block_ip', RiskTier.CONSEQUENTIAL))
    if s.false_positive:
        a.append(_c(subject, 'unblock_ip', 'Unblock the false-positive IP', 'sentinel.unblock_ip', RiskTier.BOUNDED_WRITE))
    return DecisionOpportunity(
        entity=subject, source_app="edge-sentinel", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"edge-sentinel:{subject}")


__all__ = ["SentinelSignals", "EMITTED_CAPABILITIES", "edge_sentinel_opportunity"]
