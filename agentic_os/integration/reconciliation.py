"""Integration plane — Reconciliation engine (§11).

The payout problem: a single net deposit mixes charges, refunds, fees, disputes and adjustments, and no bank
rule can deconstruct it. This engine rebuilds the components, checks the money invariant, and makes any variance
a first-class object with evidence — so "the number doesn't tie out" becomes a tracked exception, not a
month-end mystery.

Invariant (§11.3), within an explicit tolerance:

    sum(charges) - refunds - fees - disputes + adjustments  ==  payout  ==  bank deposit
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import ReconciliationItem

# How each balance-transaction type contributes to the payout net.
TYPE_SIGN: dict[str, int] = {"charge": +1, "fee": -1, "refund": -1, "dispute": -1, "adjustment": +1}


@dataclass(frozen=True)
class BalanceTxn:
    """One Stripe-shaped balance transaction (amounts are positive magnitudes in minor units/cents)."""
    id: str
    type: str                     # charge | fee | refund | dispute | adjustment
    amount: int
    currency: str = "usd"
    description: str = ""


@dataclass
class ReconciliationResult:
    reconciled: bool
    payout_id: str
    currency: str
    payout_amount: int            # the reported payout net
    computed_amount: int          # rebuilt from components
    bank_deposit: int
    component_variance: int       # payout_amount - computed_amount
    deposit_variance: int         # payout_amount - bank_deposit
    totals: dict = field(default_factory=dict)      # per-type sums
    items: list = field(default_factory=list)       # ReconciliationItem per component + a summary
    explanation: str = ""


def reconcile(payout_id: str, payout_amount: int, txns: list[BalanceTxn], bank_deposit: int,
              *, currency: str = "usd", tolerance: int = 0) -> ReconciliationResult:
    """Rebuild the payout from its balance transactions and check it ties to the reported payout and the bank
    deposit. Returns a result whose `reconciled` is True only when BOTH invariants hold within tolerance."""
    totals: dict[str, int] = {}
    for t in txns:
        if t.type not in TYPE_SIGN:
            # an unknown component type is itself a reconciliation problem — surface it, don't silently drop it
            totals.setdefault("unknown", 0)
            totals["unknown"] += t.amount
        else:
            totals[t.type] = totals.get(t.type, 0) + t.amount

    computed = sum(TYPE_SIGN.get(ty, 0) * amt for ty, amt in totals.items())
    component_variance = payout_amount - computed
    deposit_variance = payout_amount - bank_deposit
    reconciled = (abs(component_variance) <= tolerance and abs(deposit_variance) <= tolerance
                  and "unknown" not in totals)

    items = [ReconciliationItem(
        left_evidence=t.id, canonical_amount=float(TYPE_SIGN.get(t.type, 0) * t.amount),
        dimensions=(("type", t.type), ("currency", t.currency)),
        match_type="component", status="included", explanation=t.description) for t in txns]
    items.append(ReconciliationItem(
        left_evidence=f"computed:{computed}", right_evidence=f"payout:{payout_amount}|deposit:{bank_deposit}",
        canonical_amount=float(payout_amount), match_type="invariant",
        variance=float(component_variance or deposit_variance),
        status="reconciled" if reconciled else "variance",
        explanation=_explain(reconciled, totals, computed, payout_amount, bank_deposit,
                             component_variance, deposit_variance)))

    return ReconciliationResult(
        reconciled=reconciled, payout_id=payout_id, currency=currency, payout_amount=payout_amount,
        computed_amount=computed, bank_deposit=bank_deposit, component_variance=component_variance,
        deposit_variance=deposit_variance, totals=totals, items=items, explanation=items[-1].explanation)


def _explain(ok, totals, computed, payout, deposit, cvar, dvar) -> str:
    parts = " ".join(f"{k}={v}" for k, v in sorted(totals.items()))
    if ok:
        return f"reconciled: components[{parts}] → net {computed} == payout {payout} == deposit {deposit}"
    why = []
    if "unknown" in totals:
        why.append(f"unknown component type(s) totaling {totals['unknown']}")
    if cvar:
        why.append(f"components rebuild to {computed} but payout reports {payout} (Δ {cvar})")
    if dvar:
        why.append(f"payout {payout} but bank deposit {deposit} (Δ {dvar})")
    return "VARIANCE: " + "; ".join(why) + f" | components[{parts}]"
