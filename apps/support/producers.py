"""Decision producer for support (runtime-native, plan §3.4/§5).

Conversation state -> candidates over support's real capabilities. Replies are DRAFTED (a human
sends in Chatwoot, N10); escalation is urgent on an SLA breach or negative sentiment. All are bounded
writes (the operator declares none approval-gated — drafting never contacts the customer).
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("support.draft_reply", "support.resolve", "support.escalate",
                        "support.send_onboarding")


@dataclass(frozen=True)
class SupportSignals:
    conversation: str
    is_open: bool = True
    negative_sentiment: bool = False
    sla_breached: bool = False
    new_customer: bool = False
    resolvable: bool = False


def _c(conv, kind, action, cap, ev, tier, *, conf=0.75, urgency=0.4):
    return InterventionCandidate(
        source_app="support", subject=conv, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.7,
        required_capabilities=(cap,), candidate_id=f"support:{conv}:{kind}")


def support_opportunity(s: SupportSignals) -> DecisionOpportunity:
    a = []
    if s.is_open:
        a.append(_c(s.conversation, "draft_reply", "Draft a reply for human review",
                    "support.draft_reply", 0.5, RiskTier.BOUNDED_WRITE, urgency=0.5))
    if s.sla_breached or s.negative_sentiment:
        a.append(_c(s.conversation, "escalate", "Escalate to a human agent",
                    "support.escalate", 0.65, RiskTier.BOUNDED_WRITE, conf=0.8,
                    urgency=0.85 if s.sla_breached else 0.6))
    if s.resolvable:
        a.append(_c(s.conversation, "resolve", "Resolve the conversation",
                    "support.resolve", 0.45, RiskTier.BOUNDED_WRITE, urgency=0.3))
    if s.new_customer:
        a.append(_c(s.conversation, "send_onboarding", "Send the onboarding sequence",
                    "support.send_onboarding", 0.4, RiskTier.BOUNDED_WRITE, urgency=0.3))
    return DecisionOpportunity(
        entity=s.conversation, source_app="support", candidate_actions=tuple(a),
        evidence=(f"open={s.is_open}", f"sla_breached={s.sla_breached}",
                  f"negative={s.negative_sentiment}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL},
        opportunity_id=f"support:{s.conversation}")


__all__ = ["SupportSignals", "EMITTED_CAPABILITIES", "support_opportunity"]
