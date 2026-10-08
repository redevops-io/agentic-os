"""Decision producer for sky (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target sky's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("sky.optimize", "sky.launch", "sky.status", "sky.down")


@dataclass(frozen=True)
class SkySignals:
    job: str = 'train'
    job_requested: bool = False
    cost_high: bool = False
    idle: bool = False


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="sky", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"sky:{subject}:{kind}")


def sky_opportunity(s: SkySignals) -> DecisionOpportunity:
    subject = s.job
    a = []
    if s.job_requested or s.cost_high:
        a.append(_c(subject, 'optimize', 'Optimise the cloud/instance choice', 'sky.optimize', RiskTier.READ))
    if s.job_requested:
        a.append(_c(subject, 'launch', 'Launch the optimised job', 'sky.launch', RiskTier.CONSEQUENTIAL))
    if True:
        a.append(_c(subject, 'status', 'Check cluster status', 'sky.status', RiskTier.READ))
    if s.idle:
        a.append(_c(subject, 'down', 'Tear down the idle cluster', 'sky.down', RiskTier.BOUNDED_WRITE))
    return DecisionOpportunity(
        entity=subject, source_app="sky", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"sky:{subject}")


__all__ = ["SkySignals", "EMITTED_CAPABILITIES", "sky_opportunity"]
