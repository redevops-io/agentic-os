"""Outreach Intent & Timing — a domain PRODUCER for the shared decision/outcome loop
(AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §8).

The higher-value question isn't "what message do I send?" but "who is worth contacting now, and why
now?". Like CRM, Outreach is a *producer*, not its own optimizer: it turns a prospect's state into a
:class:`~agentic_os.priority_engine.DecisionOpportunity` (contact now / different channel / wait for a
trigger / monitor) and maps observed results into the shared OutcomeEvent shape. Crucially the reward
is multi-dimensional with a NEGATIVE axis — an unsubscribe or an annoyed reply — so the loop optimises
net relationship value, not raw send volume (§8). Outbound contact is consequential ⇒ approval (§19).

Together with CRM this is the cleanest cross-domain delayed-reward test of the loop: the SAME learner
attributes reward per (app, action_kind) and shifts each domain's selection toward what actually works.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate, OutcomeEvent


@dataclass(frozen=True)
class ProspectSignals:
    prospect: str
    fresh_trigger: bool = False       # a permitted signal just fired (funding, hiring, tech change)
    days_since_contact: float = 30.0
    prior_positive: bool = False      # a prior positive response in the relationship
    prior_no_response_streak: int = 0 # consecutive unanswered touches (fatigue)
    opted_out: bool = False


def _c(prospect: str, kind: str, action: str, ev: float, tier: RiskTier, *, urgency: float = 0.4) -> InterventionCandidate:
    return InterventionCandidate(
        source_app="outreach", subject=prospect, proposed_action=action, expected_value=ev,
        confidence=0.65, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.4,
        required_capabilities=(f"outreach.{kind}",), candidate_id=f"outreach:{prospect}:{kind}")


def outreach_opportunity(s: ProspectSignals) -> DecisionOpportunity:
    """Build the opportunity. An opted-out prospect yields ONLY the monitor/no-contact option (the loop
    can never select an outbound action for them). Otherwise: contact now, try another channel, wait
    for a trigger, or monitor — each with a prior value the loop will correct from outcomes."""
    a: list = []
    if not s.opted_out:
        # 'why now' is stronger with a fresh trigger; fatigue lowers the prior for another email
        email_ev = 0.7 if s.fresh_trigger else max(0.1, 0.5 - 0.12 * s.prior_no_response_streak)
        a.append(_c(s.prospect, "email_now", "Send an email now", email_ev, RiskTier.CONSEQUENTIAL,
                    urgency=0.8 if s.fresh_trigger else 0.3))
        a.append(_c(s.prospect, "linkedin_touch", "Reach out on a different channel", 0.4,
                    RiskTier.CONSEQUENTIAL))
        a.append(_c(s.prospect, "wait_for_trigger", "Hold until a stronger signal", 0.35, RiskTier.READ,
                    urgency=0.2))
    a.append(_c(s.prospect, "monitor", "Keep monitoring, do not contact", 0.15, RiskTier.READ, urgency=0.1))
    return DecisionOpportunity(
        entity=s.prospect, source_app="outreach", candidate_actions=tuple(a),
        evidence=(f"fresh_trigger={s.fresh_trigger}", f"days_since_contact={s.days_since_contact:.0f}",
                  f"no_response_streak={s.prior_no_response_streak}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL},
        uncertainty=0.6 if not s.prior_positive else 0.4, opportunity_id=f"outreach:{s.prospect}")


def outreach_outcome(action: InterventionCandidate, *, positive_reply: bool = False,
                     meeting: bool = False, negative_reply: bool = False, unsubscribed: bool = False,
                     delay_hours: float = 0.0) -> OutcomeEvent:
    """Map the observed result of an outreach action to a multi-dimensional OutcomeEvent that COUNTS
    the downside — a negative reply or an unsubscribe pushes the reward down, so the loop learns to
    contact the right prospects at the right time rather than to blast everyone."""
    dims = {}
    if positive_reply:
        dims["positive_reply"] = 1.0
    if meeting:
        dims["meeting"] = 1.0
    if negative_reply:
        dims["negative_reply"] = -1.0
    if unsubscribed:
        dims["unsubscribe"] = -1.0
    weights = {"positive_reply": 0.6, "meeting": 1.0, "negative_reply": 0.5, "unsubscribe": 1.2}
    raw = sum(weights.get(k, 1.0) * v for k, v in dims.items())
    attribution = 0.9 if delay_hours <= 24 else max(0.5, 0.9 - 0.01 * (delay_hours / 24.0))
    return OutcomeEvent(candidate_id=action.candidate_id, source_app="outreach",
                        action_kind=action.action_kind, observed_reward=raw, reward_dimensions=dims,
                        delay=delay_hours, attribution_confidence=attribution)
