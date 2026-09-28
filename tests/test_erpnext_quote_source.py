"""QUOTE_FEASIBILITY bound to the ERPNext catalog (Revenue & Execution plan §8).

Unit lane: a stub ERPNext client → a requested quote resolves through the broker as a governed
QuoteFeasibility, priced/costed/stocked from the catalog rows; an unknown item is a hard blocker; an unknown
subject is NO_MATCH. Live lane: opt-in, self-skips unless a real ERPNext is configured + reachable
(`ERPNEXT_URL` / `ERPNEXT_API_KEY` / `ERPNEXT_API_SECRET`), and only READS.
"""
from __future__ import annotations

import pytest

from agentic_os.integrations.erpnext import ErpnextClient, erpnext_from_env
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    ErpnextCatalogState, erpnext_quote_registry, revenue_synthesize,
)
from agentic_os.revenue.quote import QuoteLine
from runtime_contracts.protocol import Capability, DecisionNeed

_NOW = 1_774_000_000_000


class _StubErpnext(ErpnextClient):
    """An ErpnextClient whose REST reads are replaced by canned Item / Item Price / Bin rows."""
    def __init__(self):
        super().__init__(base_url="http://stub", api_key="k", api_secret="s")

    def connected(self) -> bool:
        return True

    def get_list(self, doctype, fields, filters=None, limit=0):
        if doctype == "Item":
            return [{"item_code": "RIM", "item_name": "Rim", "valuation_rate": 240.0, "lead_time_days": 0},
                    {"item_code": "HUB", "item_name": "Hub", "valuation_rate": 390.0, "lead_time_days": 10}]
        if doctype == "Item Price":
            return [{"item_code": "RIM", "price_list_rate": 400.0, "selling": 1},
                    {"item_code": "HUB", "price_list_rate": 600.0, "selling": 1}]
        if doctype == "Bin":
            return [{"item_code": "RIM", "actual_qty": 40.0}, {"item_code": "RIM", "actual_qty": 10.0},
                    {"item_code": "HUB", "actual_qty": 0.0}]
        return []


def _need(subject, cap=Capability.QUOTE_FEASIBILITY, **kw):
    base = dict(decision_case_id="dc1", capability=cap, question="can we fulfill this quote?",
                objective="quote_review", subject_refs=(subject,), tenant="t", min_confidence=0.0,
                as_of="2026-03-20T00:00:00Z", known_at="2026-03-20T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


# ── unit (stub client, no network) ──────────────────────────────────────────────────────────────────────
def test_catalog_maps_price_cost_onhand_leadtime():
    cat = _StubErpnext().catalog(["RIM", "HUB"])
    assert cat["RIM"].list_price_cents == 40_000 and cat["RIM"].unit_cost_cents == 24_000
    assert cat["RIM"].on_hand_qty == 50.0 and cat["RIM"].lead_time_days == 0   # two Bins summed
    assert cat["HUB"].list_price_cents == 60_000 and cat["HUB"].lead_time_days == 10


def test_quote_feasibility_resolves_against_live_catalog():
    reg = erpnext_quote_registry(_StubErpnext(),
                                 {"cust:acme": [QuoteLine("RIM", 10.0), QuoteLine("HUB", 2.0)]}, now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need("cust:acme"), synthesize=revenue_synthesize)
    assert "Quote feasible" in res.answer
    assert res.metrics["feasible"] is True and res.metrics["total_cents"] == 520_000
    assert res.total_cost == 0.0
    assert res.provider_receipts[0].provider == "internal.revenue_intelligence"


def test_unknown_item_is_a_hard_blocker():
    reg = erpnext_quote_registry(_StubErpnext(), {"cust:x": [QuoteLine("GHOST", 1.0)]}, now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need("cust:x"), synthesize=revenue_synthesize)
    assert res.metrics["feasible"] is False and "unknown item" in res.answer
    assert any("unknown item" in g for g in res.unresolved_gaps)


def test_unknown_subject_is_no_match():
    reg = erpnext_quote_registry(_StubErpnext(), {"cust:acme": [QuoteLine("RIM", 1.0)]}, now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need("cust:nobody"), synthesize=revenue_synthesize)
    assert res.answer == ""


def test_leakage_finds_no_erpnext_data():
    # ERPNext carries no pipeline, so a REVENUE_LEAKAGE need against a catalog-only source is a clean miss.
    src = ErpnextCatalogState(_StubErpnext(), {"cust:acme": [QuoteLine("RIM", 1.0)]}, now_ms=_NOW)
    assert src.leakages("cust:acme") is None


# ── live (opt-in; READ-ONLY; self-skips) ────────────────────────────────────────────────────────────────
def test_live_erpnext_quote_feasibility(monkeypatch):
    client = erpnext_from_env()
    if client is None or not client.connected():
        pytest.skip("no reachable/authorized ERPNext (set ERPNEXT_URL / ERPNEXT_API_KEY / ERPNEXT_API_SECRET)")
    codes = [it["item_code"] for it in client.get_list("Item", ["item_code"], limit=2) if it.get("item_code")]
    if not codes:
        pytest.skip("ERPNext reachable but has no Items to quote")
    reg = erpnext_quote_registry(client, {"cust:live": [QuoteLine(codes[0], 1.0)]}, now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need("cust:live"), synthesize=revenue_synthesize)
    assert res.total_cost == 0.0                        # internal provider — read-only, cost-0
    assert res.metrics.get("total_cents") is not None   # a real QuoteFeasibility resolved
