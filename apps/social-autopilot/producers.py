"""Decision producer for social-autopilot (runtime-native, plan §3.4/§5).

Content-calendar state -> candidates over social-autopilot's real capabilities. Publishing is the
approval-gated write (CONSEQUENTIAL, matching the operator); drafting is a bounded write.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("social.draft", "social.publish")


@dataclass(frozen=True)
class SocialSignals:
    channel: str = "all"
    slot_due: bool = False
    draft_ready: bool = False


def _c(channel, kind, action, cap, tier, *, ev=0.5, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="social-autopilot", subject=channel, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"social:{channel}:{kind}")


def social_opportunity(s: SocialSignals) -> DecisionOpportunity:
    a = []
    if s.slot_due:
        a.append(_c(s.channel, "draft", "Draft a post for the open calendar slot", "social.draft",
                    RiskTier.BOUNDED_WRITE, urgency=0.4))
    if s.draft_ready:
        a.append(_c(s.channel, "publish", "Publish the approved post", "social.publish",
                    RiskTier.CONSEQUENTIAL, conf=0.8, urgency=0.5))
    return DecisionOpportunity(
        entity=s.channel, source_app="social-autopilot", candidate_actions=tuple(a),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"social:{s.channel}")


__all__ = ["SocialSignals", "EMITTED_CAPABILITIES", "social_opportunity"]
