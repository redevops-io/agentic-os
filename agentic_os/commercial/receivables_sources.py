"""Receivables sources — build ReceivableCase aggregates from a live ERPNext accounts-receivable feed (§19).

The pure receivables assessment (``assess_receivable``) needs a ``ReceivableCase``; this maps live ERPNext Sales
Invoice rows into cases so dunning runs on real outstanding balances, not a demo list. Delivery/dispute/support
state is not carried on an ERPNext invoice, so those default to their benign values — meaning a genuinely overdue
invoice with no other signal resolves to COLLECT, exactly as intended. Read-only; the governed dunning send is a
separate approval-gated step. ``now_ms`` is passed in so day-counting stays deterministic (no clock read here).
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Tuple

from .receivables import DeliveryState, PaymentState, ReceivableCase

if TYPE_CHECKING:                       # type-only; no runtime coupling to the integrations adapter
    from ..integrations.erpnext import ErpnextClient

_DAY_MS = 86_400_000


def _days_overdue(due_date: str, now_ms: int) -> int:
    """Whole days a due date is in the past at ``now_ms``; 0 if no/future/unparseable date."""
    if not due_date:
        return 0
    try:
        due_ms = int(date.fromisoformat(due_date[:10]).toordinal() - date(1970, 1, 1).toordinal()) * _DAY_MS
    except (ValueError, TypeError):
        return 0
    return max(0, (now_ms - due_ms) // _DAY_MS)


def _payment_state(status: str) -> PaymentState:
    s = (status or "").lower()
    if "overdue" in s:
        return PaymentState.OVERDUE
    if "partly paid" in s:
        return PaymentState.PARTIAL
    return PaymentState.UNPAID


def receivable_cases_from_erpnext(client: "ErpnextClient", *, now_ms: int) -> Tuple[ReceivableCase, ...]:
    """Open Sales Invoices from a live ERPNext → ReceivableCases. Self-skips to () when ERPNext is unreachable.
    Deterministic: cases ordered by (-days_overdue, invoice_id)."""
    if not client.connected():
        return ()
    cases = [_case_from_row(r, now_ms=now_ms) for r in client.open_receivables()]
    cases.sort(key=lambda c: (-c.days_overdue, c.invoice_id))
    return tuple(cases)


def _case_from_row(row: Any, *, now_ms: int) -> ReceivableCase:
    days = _days_overdue(str(row.get("due_date", "")), now_ms)
    status = str(row.get("status", ""))
    state = _payment_state(status)
    # ERPNext marks "Overdue" explicitly, but a past due date with an unpaid balance is also overdue
    if state is not PaymentState.OVERDUE and days > 0:
        state = PaymentState.OVERDUE
    return ReceivableCase(
        customer=str(row.get("customer", "") or ""),
        invoice_id=str(row.get("invoice_id", "") or ""),
        amount_cents=int(row.get("outstanding_cents", 0) or 0),
        days_overdue=days,
        payment_state=state,
        delivery_state=DeliveryState.DELIVERED,     # not carried on an AR invoice → benign default
        account_context="erpnext",
        evidence=(f"erpnext:Sales Invoice/{row.get('invoice_id', '')}",))


__all__ = ["receivable_cases_from_erpnext"]
