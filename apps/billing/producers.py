"""Decision producer for billing (runtime-native, plan §3.4/§5).

Turns a customer's OBSERVED billing state (``observe.py``) into a DecisionOpportunity whose candidates
target billing's real capabilities. Dunning and refunds are approval-gated writes (CONSEQUENTIAL, matching
the operator's approval_required); the summary is a read.

Nothing here is a free-floating literal prior where Lago data exists: dunning value scales with the
overdue amount, its confidence with how complete the observation was, and its urgency with the age of
the oldest overdue invoice. Where Lago does not expose a field the signal carries an explicit entry in
``BillingSignals.fallbacks`` and the confidence is lowered accordingly (weak evidence => abstain).
The only remaining constants are the documented scales below; the outcome loop (``decision.py``
``observe_outcomes`` + the gated ``policy_promotion`` refit) is what corrects them with real data.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("billing.summary", "billing.dunning", "billing.refund")

#: "never contact a customer twice in 24h" (the invoice_recovery template's constraint, made enforceable).
DUNNING_COOLDOWN_S = float(os.environ.get("BILLING_DUNNING_COOLDOWN_S", 24 * 3600))
#: amount (minor units) at which the dunning value saturates; a documented scale, not a prediction.
REFERENCE_AMOUNT_CENTS = float(os.environ.get("BILLING_REFERENCE_AMOUNT_CENTS", 100_000))
#: confidence lost per documented fallback (a signal we had to default rather than observe).
FALLBACK_CONFIDENCE_PENALTY = 0.15


@dataclass(frozen=True)
class BillingSignals:
    """Per-account observed billing state. The first four fields are the original contract; the rest are
    filled by ``observe.build_signals`` from Lago and default to 'nothing observed'."""
    account: str
    overdue_invoices: int = 0
    days_overdue: float = 0.0                       # age of the OLDEST overdue invoice
    overcharge_detected: bool = False
    overdue_amount_cents: int = 0
    overdue_invoice_ids: Tuple[str, ...] = ()       # Lago lago_ids of the overdue invoices
    paid_invoices: int = 0                          # payment history: finalized invoices with succeeded payment
    failed_invoices: int = 0                        # payment history: finalized invoices with failed payment
    last_dunning_at: Optional[float] = None         # epoch of the last payment request / dunning we know of
    currency: str = ""
    observed_at: float = 0.0
    observation_ids: Tuple[str, ...] = ()           # the persisted Observation(s) this was built from
    fallbacks: Tuple[str, ...] = ()                 # fields Lago did not give us (documented defaults used)

    @property
    def payment_reliability(self) -> float:
        """Laplace-smoothed share of settled invoices in this account's history (0.5 with no history)."""
        return (self.paid_invoices + 1.0) / (self.paid_invoices + self.failed_invoices + 2.0)


def _c(account, kind, action, cap, ev, tier, *, conf=0.7, urgency=0.3, refs=()):
    return InterventionCandidate(
        source_app="billing", subject=account, proposed_action=action, expected_value=ev,
        confidence=max(0.0, min(1.0, conf)), urgency=max(0.0, min(1.0, urgency)),
        action_kind=kind, risk_tier=tier, reversibility=0.4, required_capabilities=(cap,),
        observation_refs=tuple(refs), candidate_id=f"billing:{account}:{kind}")


def dunning_inputs(s: BillingSignals) -> dict:
    """The scoped inputs ``billing.dunning`` needs: ONE account and exactly the invoices observed overdue."""
    return {"account_id": s.account, "invoice_ids": list(s.overdue_invoice_ids)}


def billing_opportunity(s: BillingSignals, *, now: Optional[float] = None) -> DecisionOpportunity:
    a = []
    refs = tuple(s.observation_ids)
    in_cooldown = (s.last_dunning_at is not None and now is not None
                   and 0 <= now - s.last_dunning_at < DUNNING_COOLDOWN_S)
    if s.overdue_invoices > 0 and not in_cooldown:
        amount_scale = min(1.0, (s.overdue_amount_cents or 0) / REFERENCE_AMOUNT_CENTS)
        # value: bigger overdue balances are worth chasing more; reliable payers respond to a reminder
        value = 0.2 + 0.5 * amount_scale + 0.3 * s.payment_reliability
        data_conf = 0.95 - FALLBACK_CONFIDENCE_PENALTY * len(s.fallbacks)
        conf = data_conf * (0.7 + 0.3 * s.payment_reliability)
        a.append(_c(s.account, "dunning", "Retry payment / send a reminder for the account's overdue invoice(s)",
                    "billing.dunning", value, RiskTier.CONSEQUENTIAL,
                    conf=conf, urgency=min(1.0, 0.3 + 0.02 * s.days_overdue), refs=refs))
    if s.overcharge_detected:
        a.append(_c(s.account, "refund", "Issue a credit note / refund for the overcharge",
                    "billing.refund", 0.55, RiskTier.CONSEQUENTIAL, conf=0.8, urgency=0.6, refs=refs))
    a.append(_c(s.account, "summary", "Summarise the account's billing state",
                "billing.summary", 0.2, RiskTier.READ, conf=0.9, urgency=0.1, refs=refs))
    evidence = (f"overdue_invoices={s.overdue_invoices}", f"days_overdue={s.days_overdue:.0f}",
                f"overdue_amount_cents={s.overdue_amount_cents}",
                f"paid={s.paid_invoices} failed={s.failed_invoices}",
                *(f"fallback:{f}" for f in s.fallbacks),
                *(("cooldown:dunning<24h",) if in_cooldown else ()))
    return DecisionOpportunity(
        entity=s.account, source_app="billing", candidate_actions=tuple(a), evidence=evidence,
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"billing:{s.account}")


__all__ = ["BillingSignals", "EMITTED_CAPABILITIES", "billing_opportunity", "dunning_inputs",
           "DUNNING_COOLDOWN_S"]
