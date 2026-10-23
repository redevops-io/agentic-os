"""ERPNext accounts-receivable read + ReceivableCase mapping (plan §19, finance dunning).

Unit lane: a stub ERPNext client serves canned Sales Invoice rows → open_receivables normalizes to cents →
receivable_cases_from_erpnext maps to ReceivableCases (status + due_date → payment_state/days_overdue, ordered)
→ assess_receivable then chooses COLLECT for a genuinely overdue invoice. Live lane: opt-in, self-skips.
"""
from __future__ import annotations

import pytest

from agentic_os.commercial import (
    PaymentState, ReceivableAction, assess_receivable, receivable_cases_from_erpnext,
)
from agentic_os.integrations.erpnext import ErpnextClient, erpnext_from_env

# 2026-03-20 in ms (deterministic "now" for day-counting)
_NOW = 1_774_000_000_000


class _StubErpnext(ErpnextClient):
    def __init__(self, rows):
        super().__init__(base_url="http://stub", api_key="k", api_secret="s")
        self._rows = rows

    def connected(self) -> bool:
        return True

    def get_list(self, doctype, fields, filters=None, limit=0):
        return self._rows if doctype == "Sales Invoice" else []


_ROWS = [
    {"name": "ACC-SINV-01", "customer": "Acme", "outstanding_amount": 480.0, "grand_total": 480.0,
     "due_date": "2026-01-10", "status": "Overdue", "posting_date": "2025-12-10"},
    {"name": "ACC-SINV-02", "customer": "Beta", "outstanding_amount": 120.0, "grand_total": 240.0,
     "due_date": "2026-03-25", "status": "Partly Paid", "posting_date": "2026-03-01"},
]


def test_open_receivables_normalizes_to_cents():
    rows = _StubErpnext(_ROWS).open_receivables()
    by_id = {r["invoice_id"]: r for r in rows}
    assert by_id["ACC-SINV-01"]["outstanding_cents"] == 48_000 and by_id["ACC-SINV-01"]["customer"] == "Acme"
    assert by_id["ACC-SINV-02"]["outstanding_cents"] == 12_000


def test_cases_map_status_and_days_overdue_and_order():
    cases = receivable_cases_from_erpnext(_StubErpnext(_ROWS), now_ms=_NOW)
    assert [c.invoice_id for c in cases] == ["ACC-SINV-01", "ACC-SINV-02"]   # most overdue first
    acme = cases[0]
    assert acme.payment_state is PaymentState.OVERDUE and acme.days_overdue > 0
    assert acme.amount_cents == 48_000 and acme.account_context == "erpnext"
    beta = cases[1]
    # due 2026-03-25 is after the 2026-03-20 "now" → not yet overdue, Partly Paid → PARTIAL
    assert beta.days_overdue == 0 and beta.payment_state is PaymentState.PARTIAL


def test_genuinely_overdue_case_resolves_to_collect():
    cases = receivable_cases_from_erpnext(_StubErpnext([_ROWS[0]]), now_ms=_NOW)
    assert assess_receivable(cases[0]).next_action is ReceivableAction.COLLECT


def test_self_skips_when_unreachable():
    class _Down(ErpnextClient):
        def __init__(self):
            super().__init__(base_url="http://down", api_key="k", api_secret="s")

        def connected(self) -> bool:
            return False

    assert receivable_cases_from_erpnext(_Down(), now_ms=_NOW) == ()


def test_live_erpnext_open_receivables_read_only():
    client = erpnext_from_env()
    if client is None or not client.connected():
        pytest.skip("no live ERPNext configured")
    rows = client.open_receivables(limit=5)
    assert isinstance(rows, list)
    for r in rows:
        assert r["outstanding_cents"] >= 0 and r["invoice_id"]


def test_cases_say_which_context_was_not_checked():
    """The ERPNext feed carries no delivery/dispute/support state: the case must say it assumed them."""
    cases = receivable_cases_from_erpnext(_StubErpnext(_ROWS), now_ms=_NOW)
    assert cases and all(any(e.startswith("unchecked:delivery,dispute,support") for e in c.evidence) for c in cases)
