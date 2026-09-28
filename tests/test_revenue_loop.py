"""Revenue loop — request parsing + governed quote execution (§14 tail)."""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Account, Provenance
from agentic_os.integrations.business.entity_resolution import EntityQuery
from agentic_os.revenue.flagship import plan_quote_from_request
from agentic_os.revenue.loop import InMemoryExecutor, execute_quote_plan, line_items_from_text
from agentic_os.revenue.quote import CatalogItem, QuoteLine

_NOW = 1_800_000_000_000
_CATALOG = {
    "WIDGET": CatalogItem("WIDGET", "Widget", list_price_cents=1_000, unit_cost_cents=400, on_hand_qty=10_000),
    "HUB": CatalogItem("HUB", "Hub", list_price_cents=2_000, unit_cost_cents=800, on_hand_qty=10_000),
}
_ACCOUNTS = [Account(prov=Provenance("twenty", "acct_acme"), name="Acme", domain="acme.com")]


def test_line_items_parsed_from_text() -> None:
    lines = line_items_from_text("Can you supply 300 units of Widget and 50 Hubs?", _CATALOG)
    got = {ln.item_ref: ln.qty for ln in lines}
    assert got == {"WIDGET": 300.0, "HUB": 50.0}


def _ready_plan():
    lines = line_items_from_text("supply 300 units of Widget and send pricing", _CATALOG)
    return plan_quote_from_request("supply 300 units of Widget and send pricing",
                                   entity_query=EntityQuery(domain="acme.com"), candidate_accounts=_ACCOUNTS,
                                   lines=lines, catalog=_CATALOG, now_ms=_NOW)


def test_execute_requires_approval() -> None:
    plan = _ready_plan()
    assert plan.ready and plan.approval_required
    ex = InMemoryExecutor()
    parked = execute_quote_plan(plan, executor=ex, approved=False)
    assert not parked.executed and "parked on approval" in parked.reason
    assert not ex.quotations                                   # nothing written without approval


def test_execute_when_approved_writes_back() -> None:
    plan = _ready_plan()
    ex = InMemoryExecutor()
    r = execute_quote_plan(plan, executor=ex, approved=True)
    assert r.executed and r.quote_id.startswith("QTN-") and r.opportunity_updated and r.followup_scheduled
    assert len(ex.quotations) == 1 and ex.opportunity_notes and ex.followups
    assert ex.quotations[0][1]["total_cents"] == 300_000       # 300 * $10.00


def test_execute_refuses_unready_plan() -> None:
    plan = plan_quote_from_request("just saying hi", entity_query=EntityQuery(domain="acme.com"),
                                   candidate_accounts=_ACCOUNTS, lines=[QuoteLine("WIDGET", 1)],
                                   catalog=_CATALOG, now_ms=_NOW)
    r = execute_quote_plan(plan, executor=InMemoryExecutor(), approved=True)
    assert not r.executed and "not ready" in r.reason
