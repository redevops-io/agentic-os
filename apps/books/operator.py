"""agentic-books as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive books as a capability operator — the same core ERPNext
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  books.categorize     — categorize the next uncategorized bank transaction in ERPNext
  books.reconcile      — stage a deposit↔invoice match (bank reconciliation)
  books.close          — stage the month-end close (Period Closing Voucher)  [approval gate: close]
  books.record_revenue — record onboarding subscription revenue (real Journal Entry)  [undo: reverse_entry]
  books.reverse_entry  — compensating action: cancel the revenue Journal Entry (saga undo)

Closing the books (Period Closing Voucher) carries approval_required=True (matches
modules.yaml's `approval_required: [close]`), so the runtime parks it as a HumanTask
before execution.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_books_operator() -> Operator:
    return Operator("books", [
        capability(
            "books.categorize",
            lambda inp: core.categorize(),
            provides=["transaction_categorized"],
            outputs={"transaction_categorized": "next uncategorized bank transaction tagged to an account in ERPNext"},
            side_effecting=True,
            permissions=["books:write"], estimated_value="medium", latency_ms=1200,
        ),
        capability(
            "books.reconcile",
            lambda inp: core.reconcile(),
            provides=["reconciliation_staged"],
            outputs={"reconciliation_staged": "deposit↔invoice amount match staged for posting"},
            estimated_value="medium", latency_ms=800,
        ),
        capability(
            "books.close",
            lambda inp: core.close(),
            provides=["close_staged"],
            outputs={"close_staged": "month-end close (Period Closing Voucher) staged for human approval"},
            side_effecting=True, approval_required=True,
            permissions=["books:write"], estimated_value="high", latency_ms=500,
        ),
        capability(
            "books.record_revenue",
            lambda inp: core.record_revenue(inp or {}),
            provides=["revenue_recorded"],
            outputs={"revenue_recorded": "onboarding subscription revenue posted as an ERPNext Journal Entry"},
            side_effecting=True, undo="books.reverse_entry",
            permissions=["books:write"], estimated_value="medium", latency_ms=900,
        ),
        capability(
            "books.reverse_entry",
            lambda inp: core.reverse_entry(inp or {}),
            provides=["revenue_reversed"],
            outputs={"revenue_reversed": "the revenue Journal Entry cancelled (compensating saga undo)"},
            side_effecting=True,
            permissions=["books:write"], estimated_value="medium", latency_ms=700,
        ),
    ])
