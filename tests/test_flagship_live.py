"""Flagship quote-to-order composition on LIVE data (Revenue & Execution plan §14).

Unit lane: stub Twenty (accounts) + stub ERPNext (catalog) drive the pure composition through its governed
outcomes — ready draft / observation-only / identity-gated. Live lane: opt-in, self-skips unless BOTH a real
Twenty and a real ERPNext are configured + reachable; it only READS them to construct the plan.
"""
from __future__ import annotations

import os

import pytest

from agentic_os.integrations.erpnext import ErpnextClient, erpnext_from_env
from agentic_os.integrations.twenty import TwentyClient, candidate_accounts, twenty_from_env
from agentic_os.revenue.flagship_live import plan_quote_from_request_live
from agentic_os.revenue.quote import QuoteLine

_NOW = 1_774_000_000_000
# conftest `_no_live_cores` wipes TWENTY_* per-test; ERPNEXT_* is not wiped. Snapshot both at import so the
# live lane can re-apply them.
_LIVE = {k: os.environ.get(k) for k in
         ("TWENTY_BASE_URL", "TWENTY_API_KEY", "ERPNEXT_URL", "ERPNEXT_API_KEY", "ERPNEXT_API_SECRET")}

_QUOTE_TEXT = "Hi, please send a quote for 2 units of RIM — need pricing and availability."


class _StubTwenty(TwentyClient):
    def __init__(self):
        super().__init__(base_url="http://stub", api_key="x")

    def companies(self, *, limit: int = 60):
        return [{"id": "c1", "name": "ACME Corp", "domainName": {"primaryLinkUrl": "acme.com"}},
                {"id": "c2", "name": "Beta LLC", "domainName": {"primaryLinkUrl": "beta.io"}}]


class _StubErpnext(ErpnextClient):
    def __init__(self):
        super().__init__(base_url="http://stub", api_key="k", api_secret="s")

    def connected(self):
        return True

    def get_list(self, doctype, fields, filters=None, limit=0):
        if doctype == "Item":
            return [{"item_code": "RIM", "item_name": "Rim", "valuation_rate": 240.0, "lead_time_days": 10}]
        if doctype == "Item Price":
            return [{"item_code": "RIM", "price_list_rate": 400.0, "selling": 1}]
        if doctype == "Bin":
            return [{"item_code": "RIM", "actual_qty": 0.0}]
        return []


# ── unit (stubs) ────────────────────────────────────────────────────────────────────────────────────────
def test_live_wiring_produces_ready_governed_plan():
    # name + domain → a RESOLVED match (a name alone caps at PROBABLE by design, §12)
    plan = plan_quote_from_request_live(
        _QUOTE_TEXT, lines=[QuoteLine("RIM", 2.0)], twenty_client=_StubTwenty(), erpnext_client=_StubErpnext(),
        now_ms=_NOW, customer_name="ACME Corp", customer_domain="acme.com")
    assert plan.ready is True and plan.approval_required is True
    assert plan.entity is not None and plan.entity.state.value == "RESOLVED"
    assert plan.feasibility is not None and plan.feasibility.feasible is True
    assert plan.as_dict()["draft_quote_plan"]["total_cents"] == 80_000    # 2 * $400 (10d lead ⇒ fulfillable)


def test_non_quote_text_is_observation_only():
    plan = plan_quote_from_request_live(
        "thanks, the invoice looks correct", lines=[QuoteLine("RIM", 1.0)], twenty_client=_StubTwenty(),
        erpnext_client=_StubErpnext(), now_ms=_NOW, customer_name="ACME Corp")
    assert plan.ready is False and plan.approval_required is False and "observation only" in plan.reason


def test_unresolved_customer_cannot_auto_draft():
    plan = plan_quote_from_request_live(
        _QUOTE_TEXT, lines=[QuoteLine("RIM", 1.0)], twenty_client=_StubTwenty(), erpnext_client=_StubErpnext(),
        now_ms=_NOW, customer_name="Nonexistent Ltd")
    assert plan.ready is False and "identity" in plan.reason


# ── live (opt-in; READ-ONLY; self-skips) ────────────────────────────────────────────────────────────────
def test_live_flagship_end_to_end(monkeypatch):
    if not all(_LIVE.values()):
        pytest.skip("need live Twenty + ERPNext creds (TWENTY_* / ERPNEXT_*)")
    for k, v in _LIVE.items():
        monkeypatch.setenv(k, v)
    tw, erp = twenty_from_env(), erpnext_from_env()
    if tw is None or erp is None or not erp.connected() or not tw.companies(limit=1):
        pytest.skip("Twenty/ERPNext configured but not reachable")
    # pick a UNIQUE live company name → a deterministic RESOLVED match
    names = [a.name for a in candidate_accounts(tw, limit=200) if a.name]
    unique = next((n for n in names if names.count(n) == 1), "")
    if not unique:
        pytest.skip("no uniquely-named live company to resolve against")
    codes = [it["item_code"] for it in erp.get_list("Item", ["item_code"], limit=1) if it.get("item_code")]
    if not codes:
        pytest.skip("ERPNext has no Items to quote")
    # demo Twenty companies match on name alone (→ PROBABLE); the flagship allows a policy-approved PROBABLE
    # to drive the draft (§12), which the Mission gate still parks on approval.
    plan = plan_quote_from_request_live(
        f"Please send a quote for 2 units of {codes[0]} — pricing and availability?",
        lines=[QuoteLine(codes[0], 2.0)], twenty_client=tw, erpnext_client=erp, now_ms=_NOW,
        customer_name=unique, allow_probable_entity=True)
    assert plan.intent.intent.value == "QUOTE_REQUEST"
    assert plan.entity is not None and plan.entity.state.value in ("RESOLVED", "PROBABLE")
    assert plan.ready is True and plan.approval_required is True
    assert plan.as_dict()["draft_quote_plan"]["total_cents"] is not None
