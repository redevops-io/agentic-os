"""Decision producer for billing (runtime-native, plan §3.4/§5).

Turns a customer's billing state into a DecisionOpportunity whose candidates target billing's real
capabilities. Dunning and refunds are approval-gated writes (CONSEQUENTIAL, matching the operator's
approval_required); the summary is a read. Priors are heuristics the outcome loop corrects.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("billing.summary", "billing.dunning", "billing.refund")


@dataclass(frozen=True)
class BillingSignals:
    account: str
    overdue_invoices: int = 0
    days_overdue: float = 0.0
    overcharge_detected: bool = False


def _c(account, kind, action, cap, ev, tier, *, conf=0.7, urgency=0.3):
    return InterventionCandidate(
        source_app="billing", subject=account, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.4,
        required_capabilities=(cap,), candidate_id=f"billing:{account}:{kind}")


def billing_opportunity(s: BillingSignals) -> DecisionOpportunity:
    a = []
    if s.overdue_invoices > 0:
        a.append(_c(s.account, "dunning", "Send a payment reminder for the overdue invoice(s)",
                    "billing.dunning", 0.6 + min(0.3, 0.02 * s.days_overdue), RiskTier.CONSEQUENTIAL,
                    conf=0.85, urgency=min(1.0, 0.3 + 0.02 * s.days_overdue)))
    if s.overcharge_detected:
        a.append(_c(s.account, "refund", "Issue a credit note / refund for the overcharge",
                    "billing.refund", 0.55, RiskTier.CONSEQUENTIAL, conf=0.8, urgency=0.6))
    a.append(_c(s.account, "summary", "Summarise the account's billing state",
                "billing.summary", 0.2, RiskTier.READ, conf=0.9, urgency=0.1))
    return DecisionOpportunity(
        entity=s.account, source_app="billing", candidate_actions=tuple(a),
        evidence=(f"overdue_invoices={s.overdue_invoices}", f"days_overdue={s.days_overdue:.0f}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"billing:{s.account}")


__all__ = ["BillingSignals", "EMITTED_CAPABILITIES", "billing_opportunity"]
