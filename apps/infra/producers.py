"""Decision producer for infra (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target infra's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("infra.plan", "infra.provision", "infra.configure", "infra.drift", "infra.rollback_release")


@dataclass(frozen=True)
class InfraSignals:
    stack: str = 'app'
    deploy_requested: bool = False
    drift_detected: bool = False
    verify_failed: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="infra", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"infra:{subject}:{kind}")


def infra_opportunity(s: InfraSignals) -> DecisionOpportunity:
    subject = s.stack
    a = []
    if s.deploy_requested or s.drift_detected:
        a.append(_c(subject, 'plan', 'Plan the infrastructure delta', 'infra.plan', RiskTier.READ))
    if s.deploy_requested:
        a.append(_c(subject, 'provision', 'Apply the planned infrastructure', 'infra.provision', RiskTier.CONSEQUENTIAL))
    if s.deploy_requested:
        a.append(_c(subject, 'configure', 'Configure the provisioned stack', 'infra.configure', RiskTier.BOUNDED_WRITE))
    if s.drift_detected:
        a.append(_c(subject, 'drift', 'Report configuration drift', 'infra.drift', RiskTier.READ))
    if s.verify_failed:
        a.append(_c(subject, 'rollback_release', 'Roll back the failed release', 'infra.rollback_release', RiskTier.BOUNDED_WRITE))
    return DecisionOpportunity(
        entity=subject, source_app="infra", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"infra:{subject}")


__all__ = ["InfraSignals", "EMITTED_CAPABILITIES", "infra_opportunity"]
