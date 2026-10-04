"""Experiment A (§40.A) — Stripe payout → accounting reconciliation, against synthetic data.

Proves the obligation plane generalizes to a hard many-to-one money workflow: a clean payout reconciles and
posts a verified clearing entry (receipt); a broken payout (missing component, deposit mismatch, unknown type)
is caught as a durable variance exception and NEVER posts a bad entry. Offline, deterministic — no live money.
"""
from __future__ import annotations

from agentic_os.integration import ExceptionCategory, ObligationStatus
from agentic_os.integration.experiments.payout_reconciliation import run_payout_reconciliation
from agentic_os.integration.reconciliation import BalanceTxn, reconcile
from agentic_os.integration.testkit import seed_providers, synthetic_payout


# ── the reconciliation invariant itself ──────────────────────────────────────────────────────────────────
def test_invariant_holds_for_clean_components():
    txns = [BalanceTxn("c", "charge", 10000), BalanceTxn("f", "fee", 320), BalanceTxn("r", "refund", 1500)]
    r = reconcile("po", payout_amount=10000 - 320 - 1500, txns=txns, bank_deposit=10000 - 320 - 1500)
    assert r.reconciled and r.component_variance == 0 and r.deposit_variance == 0
    assert "reconciled" in r.explanation


def test_invariant_catches_component_and_deposit_variance():
    txns = [BalanceTxn("c", "charge", 10000), BalanceTxn("f", "fee", 320)]
    # payout claims 9680 but we also under-deposit → both variances
    r = reconcile("po", payout_amount=9680, txns=txns, bank_deposit=9000)
    assert not r.reconciled and r.deposit_variance == 680 and "VARIANCE" in r.explanation


# ── end-to-end: clean payout reconciles + posts a verified clearing entry ────────────────────────────────
def test_clean_payout_reconciles_and_posts_verified_entry():
    fx = synthetic_payout(seed=1, charges=5, with_refund=True, with_dispute=True)
    stripe, bank, accounting = seed_providers(fx)
    out = run_payout_reconciliation(stripe, bank, accounting, fx.payout_id)
    assert out.reconciliation.reconciled
    assert out.ok and out.discharge.obligation.status == ObligationStatus.SATISFIED
    # the receipt proves the ledger entry was OBSERVED, not just POSTed
    assert out.discharge.receipt.observed_state["journal_entry"]["payout"] == fx.payout_id
    assert out.discharge.receipt.observed_state["journal_entry"]["balanced"] is True
    # and the entry actually landed in accounting
    je = accounting.read_object("journal_entry", f"je_{fx.payout_id}")
    assert je is not None and je.normalized_fields["amount"] == fx.net_amount


# ── the money tests: broken payouts are caught, nothing posted ───────────────────────────────────────────
def test_missing_component_is_caught_as_variance():
    fx = synthetic_payout(seed=2, break_mode="missing_txn")
    stripe, bank, accounting = seed_providers(fx)
    out = run_payout_reconciliation(stripe, bank, accounting, fx.payout_id)
    assert not out.reconciliation.reconciled and out.exception
    assert out.exception.category == ExceptionCategory.RECONCILIATION_VARIANCE
    # no clearing entry was posted for an unbalanced payout
    assert accounting.read_object("journal_entry", f"je_{fx.payout_id}") is None
    assert out.exception.business_impact and out.exception.recommended_action


def test_deposit_mismatch_is_caught():
    fx = synthetic_payout(seed=3, break_mode="deposit_mismatch")
    stripe, bank, accounting = seed_providers(fx)
    out = run_payout_reconciliation(stripe, bank, accounting, fx.payout_id)
    assert out.exception and out.exception.category == ExceptionCategory.RECONCILIATION_VARIANCE
    assert out.reconciliation.deposit_variance == 137


def test_unknown_component_type_is_caught():
    fx = synthetic_payout(seed=4, break_mode="unknown_type")
    stripe, bank, accounting = seed_providers(fx)
    out = run_payout_reconciliation(stripe, bank, accounting, fx.payout_id)
    assert out.exception and "unknown component" in out.exception.detail
    assert accounting.read_object("journal_entry", f"je_{fx.payout_id}") is None


def test_idempotent_reconciliation_posts_once():
    fx = synthetic_payout(seed=5)
    stripe, bank, accounting = seed_providers(fx)
    run_payout_reconciliation(stripe, bank, accounting, fx.payout_id)
    run_payout_reconciliation(stripe, bank, accounting, fx.payout_id)   # replay
    creates = [c for c in accounting.calls if c[0] == "create" and c[1] == "journal_entry"]
    # idempotency_key = payout_id → the ledger entry is created once, not duplicated on replay
    assert len([e for e in accounting._store.get("journal_entry", {})]) == 1
