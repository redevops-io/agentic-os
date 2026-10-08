"""Decision producer for compliance (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target compliance's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("compliance.scan", "compliance.explain", "compliance.remediate", "compliance.file_consent")


@dataclass(frozen=True)
class ComplianceSignals:
    target: str = 'host'
    open_findings: int = 0
    scan_due: bool = False
    consent_needed: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="compliance", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"compliance:{subject}:{kind}")


def compliance_opportunity(s: ComplianceSignals) -> DecisionOpportunity:
    subject = s.target
    a = []
    if s.scan_due:
        a.append(_c(subject, 'scan', 'Run a compliance scan', 'compliance.scan', RiskTier.READ))
    if s.open_findings > 0:
        a.append(_c(subject, 'explain', 'Explain the open findings', 'compliance.explain', RiskTier.READ))
    if s.open_findings > 0:
        a.append(_c(subject, 'remediate', 'Remediate the finding', 'compliance.remediate', RiskTier.CONSEQUENTIAL))
    if s.consent_needed:
        a.append(_c(subject, 'file_consent', 'File the required consent record', 'compliance.file_consent', RiskTier.CONSEQUENTIAL))
    return DecisionOpportunity(
        entity=subject, source_app="compliance", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"compliance:{subject}")


__all__ = ["ComplianceSignals", "EMITTED_CAPABILITIES", "compliance_opportunity"]
