"""Receivables Intelligence — cross-system reasoning, not a blind reminder (plan §19, P8)."""
from __future__ import annotations

from agentic_os.commercial import (
    DeliveryState, DisputeState, PaymentState, ReceivableAction, ReceivableCase, SupportState,
    assess_receivable, collection_expected_transitions,
)


def _case(**kw):
    base = dict(customer="acme", invoice_id="inv1", amount_cents=100000, days_overdue=20)
    base.update(kw)
    return ReceivableCase(**base)


def test_paid_needs_no_action():
    a = assess_receivable(_case(payment_state=PaymentState.PAID))
    assert a.next_action is ReceivableAction.NONE


def test_delivery_incomplete_takes_priority_over_collection():
    a = assess_receivable(_case(delivery_state=DeliveryState.PENDING))
    assert a.next_action is ReceivableAction.RESOLVE_DELIVERY and a.probable_cause == "delivery_incomplete"


def test_dispute_then_invoice_error_then_support_ordering():
    assert assess_receivable(_case(dispute_state=DisputeState.OPEN)).next_action is ReceivableAction.RESOLVE_DISPUTE
    assert assess_receivable(_case(invoice_error=True)).next_action is ReceivableAction.CORRECT_INVOICE
    assert assess_receivable(_case(support_state=SupportState.OPEN_BLOCKER)).next_action is ReceivableAction.ESCALATE


def test_promised_payment_waits_not_chases():
    a = assess_receivable(_case(promised_future_payment=True))
    assert a.next_action is ReceivableAction.WAIT and a.probable_cause == "promised_payment"


def test_genuinely_overdue_collects_and_materiality():
    a = assess_receivable(_case(days_overdue=30, amount_cents=50000))
    assert a.next_action is ReceivableAction.COLLECT
    assert a.materiality == round(500.0 * 30, 2)   # outstanding × days overdue


def test_collection_lifecycle_transitions():
    ts = collection_expected_transitions(_case())
    assert [t.expected_to_state for t in ts] == ["actioned", "promised", "paid", "verified"]
