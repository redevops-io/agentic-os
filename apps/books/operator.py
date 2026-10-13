"""agentic-books as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive books as a capability operator — the same core ERPNext
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  books.categorize     — tag the next uncategorized bank transaction (tag_only)  [approval-gated; stages a preview]
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
    LEDGER = "books:ledger"   # serialize journal posts (double-entry integrity); reads run free
    return Operator("books", [
        capability(
            "books.categorize",
            lambda inp: core.categorize(approval=(inp or {}).get("_approval")),
            provides=["transaction_categorized"],
            outputs={"transaction_categorized": "next uncategorized bank transaction tagged in ERPNext "
                                                "(mode: tag_only - reference_number tag, no account allocation); "
                                                "staged preview until approved"},
            side_effecting=True, approval_required=True,
            permissions=["books:write"], estimated_value="medium", latency_ms=1200,
            concurrency_mode="exclusive", resource_keys=[LEDGER],
        ),
        capability(
            "books.reconcile",
            lambda inp: core.reconcile(),
            provides=["reconciliation_staged"],
            outputs={"reconciliation_staged": "deposit↔invoice amount match staged for posting"},
            estimated_value="medium", latency_ms=800,
            concurrency_mode="read_only",
        ),
        capability(
            "books.close",
            lambda inp: core.close(inp or {}),
            provides=["close_staged"],
            outputs={"close_staged": "month-end close (Period Closing Voucher) staged for human approval"},
            side_effecting=True, approval_required=True,
            permissions=["books:write"], estimated_value="high", latency_ms=500,
            concurrency_mode="exclusive", resource_keys=[LEDGER],
        ),
        capability(
            "books.record_revenue",
            lambda inp: core.record_revenue(inp or {}),    # approval gate lives in core (_approval)
            provides=["revenue_recorded"],
            outputs={"revenue_recorded": "onboarding subscription revenue posted as an ERPNext Journal Entry"},
            side_effecting=True, approval_required=True, undo="books.reverse_entry",
            permissions=["books:write"], estimated_value="medium", latency_ms=900,
            concurrency_mode="exclusive", resource_keys=[LEDGER],
        ),
        capability(
            "books.reverse_entry",
            lambda inp: core.reverse_entry(inp or {}),     # approval gate lives in core (_approval)
            provides=["revenue_reversed"],
            outputs={"revenue_reversed": "the revenue Journal Entry cancelled (compensating saga undo)"},
            side_effecting=True, approval_required=True,
            permissions=["books:write"], estimated_value="medium", latency_ms=700,
            concurrency_mode="exclusive", resource_keys=[LEDGER],
        ),
    ])
