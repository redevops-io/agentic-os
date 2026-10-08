"""Decision producer for growth-assistant (runtime-native, plan §3.4/§5).

Domain signals -> a DecisionOpportunity whose candidates target growth-assistant's own registered capabilities,
with risk tiers aligned to each capability's approval flag (approval-gated writes are CONSEQUENTIAL).
Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("assistant.playbook", "assistant.founder_content", "assistant.ask")


@dataclass(frozen=True)
class AssistantSignals:
    founder: str = 'founder'
    needs_playbook: bool = False
    needs_content: bool = False
    question: str = ''


def _c(subject, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="growth-assistant", subject=subject, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"growth-assistant:{subject}:{kind}")


def growth_assistant_opportunity(s: AssistantSignals) -> DecisionOpportunity:
    subject = s.founder
    a = []
    if s.needs_playbook:
        a.append(_c(subject, 'playbook', 'Draft a growth playbook', 'assistant.playbook', RiskTier.READ))
    if s.needs_content:
        a.append(_c(subject, 'founder_content', 'Draft founder content', 'assistant.founder_content', RiskTier.BOUNDED_WRITE))
    if bool(s.question):
        a.append(_c(subject, 'ask', "Answer the founder's growth question", 'assistant.ask', RiskTier.READ))
    return DecisionOpportunity(
        entity=subject, source_app="growth-assistant", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"growth-assistant:{subject}")


__all__ = ["AssistantSignals", "EMITTED_CAPABILITIES", "growth_assistant_opportunity"]
