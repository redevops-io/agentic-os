"""CRM Next-Best-Action — a domain PRODUCER for the shared decision/outcome loop
(AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §3).

Unlike the five deterministic detector kernels, "which action produces a better commercial outcome?"
cannot be validated offline — so CRM does NOT get its own bespoke optimizer. It is a *producer*: it
turns a deal's state into a :class:`~agentic_os.priority_engine.DecisionOpportunity` (several candidate
actions with prior predictions and correct risk tiers), and it maps observed commercial events
(reply → meeting → conversion → loss) into the shared :class:`~agentic_os.priority_engine.OutcomeEvent`
shape (multi-dimensional, delayed, attributed). The shared runtime owns selection (``select_action``)
and learning (``UtilityModel``); this file owns only the domain semantics.

The predicted values here are deliberately *priors* (heuristics), not truth — the whole point of the
loop is that observed outcomes correct which action actually works at each stage. Outbound
communication is consequential, so it routes to human approval (§19).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate, OutcomeEvent


@dataclass(frozen=True)
class DealSignals:
    account: str
    stage: str = "engaged"           # new | engaged | stalled | proposal_sent | negotiation
    days_since_touch: float = 3.0
    engagement: float = 0.5          # 0..1 recent engagement
    buying_signal: bool = False      # a new intent signal (pricing page, demo request, …)
    open_technical_question: bool = False
    renewal_days: Optional[float] = None   # days to renewal, if this is an existing customer


def _c(account: str, kind: str, action: str, ev: float, tier: RiskTier, *, conf: float = 0.7,
       urgency: float = 0.4) -> InterventionCandidate:
    return InterventionCandidate(
        source_app="crm", subject=account, proposed_action=action, expected_value=ev, confidence=conf,
        urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(f"crm.{kind}",), candidate_id=f"crm:{account}:{kind}")


def next_best_action(s: DealSignals) -> DecisionOpportunity:
    """Build the opportunity: the candidate actions that make sense for this deal's state, each with a
    PRIOR predicted value (a heuristic the loop will correct) and its correct risk tier."""
    a: list = []
    if s.open_technical_question:
        a.append(_c(s.account, "answer_question", "Answer the open technical question", 0.7,
                    RiskTier.CONSEQUENTIAL, conf=0.8, urgency=0.7))
    if s.stage in ("engaged", "negotiation") or s.buying_signal:
        a.append(_c(s.account, "send_proposal", "Send the technical deployment proposal", 0.8,
                    RiskTier.CONSEQUENTIAL, urgency=0.7 if s.buying_signal else 0.4))
    a.append(_c(s.account, "schedule_call", "Propose a call", 0.5, RiskTier.CONSEQUENTIAL))
    if s.stage in ("new", "stalled") or s.engagement < 0.4:
        a.append(_c(s.account, "nurture_email", "Send a nurture email", 0.4, RiskTier.CONSEQUENTIAL))
    if s.renewal_days is not None and s.renewal_days <= 60:
        a.append(_c(s.account, "renewal_outreach", "Start the renewal conversation", 0.75,
                    RiskTier.CONSEQUENTIAL, urgency=0.8))
    # 'monitor' is a low-risk non-action the loop can prefer when nothing outbound is worth it
    a.append(_c(s.account, "monitor", "Keep watching, no outreach yet", 0.15, RiskTier.READ, urgency=0.1))
    return DecisionOpportunity(
        entity=s.account, source_app="crm", candidate_actions=tuple(a),
        evidence=(f"stage={s.stage}", f"engagement={s.engagement:.2f}",
                  f"days_since_touch={s.days_since_touch:.0f}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL},   # never a CRITICAL action from CRM auto
        uncertainty=0.5 if s.stage == "new" else 0.3,
        opportunity_id=f"crm:{s.account}")


# ── outcome mapping: commercial events → the shared OutcomeEvent shape ────────────────
def crm_outcome(action: InterventionCandidate, *, replied: bool = False, meeting: bool = False,
                converted: bool = False, lost: bool = False, unsubscribed: bool = False,
                delay_hours: float = 0.0) -> OutcomeEvent:
    """Map the observed commercial result of a CRM action to a multi-dimensional, delayed, attributed
    OutcomeEvent. Conversion is attributed less confidently than an immediate reply (many things move a
    deal); a loss or an unsubscribe is a NEGATIVE dimension so the loop can't just maximise volume."""
    dims = {}
    if replied:
        dims["reply"] = 1.0
    if meeting:
        dims["meeting"] = 1.0
    if converted:
        dims["conversion"] = 1.0
    if lost:
        dims["loss"] = -1.0
    if unsubscribed:
        dims["unsubscribe"] = -1.0
    # weight: a conversion is worth far more than a reply; attribution weakens with the outcome's delay
    weights = {"reply": 0.2, "meeting": 0.5, "conversion": 1.0, "loss": 0.6, "unsubscribe": 0.4}
    raw = sum(weights.get(k, 1.0) * v for k, v in dims.items())   # attribution-free domain reward
    attribution = 0.9 if delay_hours <= 24 else max(0.4, 0.9 - 0.01 * (delay_hours / 24.0))
    # observed_reward is the attribution-FREE scalar; scalar_reward() applies attribution once, so the
    # shared learner needs no CRM-specific weight config.
    return OutcomeEvent(candidate_id=action.candidate_id, source_app="crm", action_kind=action.action_kind,
                        observed_reward=raw, reward_dimensions=dims, delay=delay_hours,
                        attribution_confidence=attribution)
