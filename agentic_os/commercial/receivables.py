"""Receivables Intelligence — reason about an overdue invoice from cross-system evidence (plan §19, P8).

The audit found a deterministic receivables Mission but no first-class ReceivableCase aggregate — and the plan's
whole point is to do better than "invoice overdue → send reminder". This adds the aggregate + a deterministic
assessment that reasons across delivery/dispute/support/promised-payment state before choosing an action, and the
collection lifecycle transitions. Pure + deterministic; real Lago/ERPNext/CRM/support reads + the governed dunning
Mission (Obligation-verified send) bind in the enterprise overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Tuple

from ..reliability import ExpectedTransition


class PaymentState(str, Enum):
    PAID = "paid"; PARTIAL = "partial"; UNPAID = "unpaid"; OVERDUE = "overdue"


class DeliveryState(str, Enum):
    DELIVERED = "delivered"; PARTIAL = "partial"; PENDING = "pending"; FAILED = "failed"


class DisputeState(str, Enum):
    NONE = "none"; OPEN = "open"; RESOLVED = "resolved"


class SupportState(str, Enum):
    NONE = "none"; OPEN_BLOCKER = "open_blocker"; RESOLVED = "resolved"


class ReceivableAction(str, Enum):
    COLLECT = "collect"; RESOLVE_DELIVERY = "resolve_delivery"; RESOLVE_DISPUTE = "resolve_dispute"
    CORRECT_INVOICE = "correct_invoice"; ESCALATE = "escalate"; WAIT = "wait"; NONE = "none"


@dataclass(frozen=True)
class ReceivableCase:
    customer: str
    invoice_id: str
    amount_cents: int = 0
    days_overdue: int = 0
    payment_state: PaymentState = PaymentState.UNPAID
    delivery_state: DeliveryState = DeliveryState.DELIVERED
    dispute_state: DisputeState = DisputeState.NONE
    support_state: SupportState = SupportState.NONE
    invoice_error: bool = False
    promised_future_payment: bool = False
    account_context: str = ""
    evidence: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceivableAssessment:
    probable_cause: str
    next_action: ReceivableAction
    rationale: str = ""
    materiality: float = 0.0


def assess_receivable(case: ReceivableCase) -> ReceivableAssessment:
    """Decide the right next action from cross-system state (§19), in priority order — NOT a blind reminder.
    Deterministic. Materiality = outstanding amount × days overdue."""
    materiality = round((case.amount_cents / 100.0) * max(case.days_overdue, 1), 2)

    def R(cause, action, why):
        return ReceivableAssessment(probable_cause=cause, next_action=action, rationale=why, materiality=materiality)

    if case.payment_state is PaymentState.PAID:
        return R("paid", ReceivableAction.NONE, "already paid")
    if case.delivery_state in (DeliveryState.PENDING, DeliveryState.PARTIAL, DeliveryState.FAILED):
        return R("delivery_incomplete", ReceivableAction.RESOLVE_DELIVERY,
                 "customer may be withholding payment until delivery completes")
    if case.dispute_state is DisputeState.OPEN:
        return R("open_dispute", ReceivableAction.RESOLVE_DISPUTE, "an open dispute blocks payment")
    if case.invoice_error:
        return R("invoice_error", ReceivableAction.CORRECT_INVOICE, "the invoice itself is wrong")
    if case.support_state is SupportState.OPEN_BLOCKER:
        return R("support_blocker", ReceivableAction.ESCALATE, "an unresolved support problem is blocking payment")
    if case.promised_future_payment:
        return R("promised_payment", ReceivableAction.WAIT, "customer promised a payment date; do not chase yet")
    if case.days_overdue > 0 or case.payment_state is PaymentState.OVERDUE:
        return R("genuinely_overdue", ReceivableAction.COLLECT, "no blocker found; collect")
    return R("not_yet_due", ReceivableAction.WAIT, "not overdue")


COLLECTION_STAGES: Tuple[str, ...] = ("identified", "actioned", "promised", "paid", "verified")


def collection_expected_transitions(case: ReceivableCase, *, deadline: int = 0) -> Tuple[ExpectedTransition, ...]:
    out = []
    for i in range(1, len(COLLECTION_STAGES)):
        frm, to = COLLECTION_STAGES[i - 1], COLLECTION_STAGES[i]
        out.append(ExpectedTransition(subject=f"receivable:{case.invoice_id}:{to}", expected_to_state=to,
                                      from_state=frm, expected_by=deadline, failure_states=("written_off",),
                                      remediation_policy=f"remediate_{to}", impact=case.amount_cents / 100.0))
    return tuple(out)


__all__ = ["PaymentState", "DeliveryState", "DisputeState", "SupportState", "ReceivableAction",
           "ReceivableCase", "ReceivableAssessment", "assess_receivable", "COLLECTION_STAGES",
           "collection_expected_transitions"]
