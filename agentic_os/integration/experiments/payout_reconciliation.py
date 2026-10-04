"""Experiment A (§40.A) — Stripe payout → accounting reconciliation.

Exercises: many-to-one reconciliation, money, aggregation lineage, independent verification, and exceptions —
all against synthetic data. The flow proves the obligation plane end-to-end:

    payout.created
      → read payout + balance transactions (evidence)
      → reconcile: rebuild net, check invariant vs payout AND bank deposit
      → reconciled?  post a BALANCED clearing journal entry to accounting
      → Obligation: the journal entry must EXIST and be balanced (verified by read-back) → receipt
      → variance?   do NOT post; raise a durable RECONCILIATION_VARIANCE exception with evidence

A successful POST to the ledger does not end the story — the obligation is satisfied only when the entry is read
back. If the books don't tie out, we never post a bad entry; we surface a tracked variance instead.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts import ExceptionCategory, IntegrationException, Obligation, RetryPolicy
from ..obligations import DischargeResult, ObligationEngine
from ..provider import ActionResult, IntegrationProvider
from ..reconciliation import BalanceTxn, ReconciliationResult, reconcile


@dataclass
class PayoutReconOutcome:
    reconciliation: ReconciliationResult
    discharge: "DischargeResult | None" = None     # set when reconciled + posted + verified
    exception: "IntegrationException | None" = None  # set on variance (nothing posted)

    @property
    def ok(self) -> bool:
        return self.discharge is not None and self.discharge.satisfied


def run_payout_reconciliation(stripe: IntegrationProvider, bank: IntegrationProvider,
                              accounting: IntegrationProvider, payout_id: str,
                              *, tolerance: int = 0) -> PayoutReconOutcome:
    # 1. evidence — the payout and its balance transactions
    payout_obs = stripe.read_object("payout", payout_id)
    if payout_obs is None:
        raise ValueError(f"unknown payout {payout_id!r}")
    payout_amount = int(payout_obs.normalized_fields["amount"])
    currency = payout_obs.normalized_fields.get("currency", "usd")
    txns = []
    for txn_id in payout_obs.normalized_fields.get("balance_transactions", []):
        t = stripe.read_object("balance_transaction", txn_id)
        if t is not None:
            f = t.normalized_fields
            txns.append(BalanceTxn(f["id"], f["type"], int(f["amount"]), f.get("currency", currency),
                                   f.get("description", "")))

    # 2. the bank deposit that actually landed
    deposits = bank.search_objects("deposit", {"payout_ref": payout_id})
    bank_deposit = int(deposits[0].normalized_fields["amount"]) if deposits else 0

    # 3. reconcile — rebuild net, check both invariants
    result = reconcile(payout_id, payout_amount, txns, bank_deposit, currency=currency, tolerance=tolerance)

    # 4a. variance → durable exception; never post an unbalanced entry
    if not result.reconciled:
        exc = IntegrationException(
            category=ExceptionCategory.RECONCILIATION_VARIANCE, workflow_id="payout-reconciliation",
            affected_entities=(payout_id,), detail=result.explanation,
            business_impact=f"{abs(result.component_variance or result.deposit_variance)} {currency} unexplained",
            recommended_action="investigate the missing/extra component before posting to the ledger",
            evidence=tuple(i.left_evidence for i in result.items if i.left_evidence))
        return PayoutReconOutcome(reconciliation=result, exception=exc)

    # 4b. reconciled → post a balanced clearing entry, obligated to be observed in the ledger
    def _post() -> ActionResult:
        return accounting.create_object("journal_entry", {
            "id": f"je_{payout_id}", "payout": payout_id, "amount": payout_amount, "currency": currency,
            "balanced": True, "lines": {k: v for k, v in result.totals.items()}},
            idempotency_key=payout_id)

    obligation = Obligation(
        trigger="stripe.payout.reconciled", source_resource="stripe", destination_resource="accounting",
        entity_refs=(payout_id,), workflow_id="payout-reconciliation", retry_policy=RetryPolicy(max_attempts=2),
        expected_state={"journal_entry": {"payout": payout_id, "amount": payout_amount, "balanced": True}})
    discharge = ObligationEngine().discharge(
        obligation, accounting, action=_post, targets={"journal_entry": f"je_{payout_id}"},
        authority="svc@finance")
    return PayoutReconOutcome(reconciliation=result, discharge=discharge)
