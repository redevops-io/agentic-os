"""Revenue Intelligence — quote feasibility (§8): can we fulfill, by when, at what price/margin, what blocks."""
from __future__ import annotations

from agentic_os.revenue.quote import CatalogItem, QuoteLine, assess_quote_feasibility

_NOW = 1_800_000_000_000
_DAY = 86_400_000
_CATALOG = {
    "RIM": CatalogItem("RIM", "Wheel rim", list_price_cents=10_000, unit_cost_cents=6_000, on_hand_qty=100),
    "HUB": CatalogItem("HUB", "Hub", list_price_cents=20_000, unit_cost_cents=18_500, on_hand_qty=0,
                       lead_time_days=7, substitute_refs=("HUB2",)),
    "GEM": CatalogItem("GEM", "Rare part", list_price_cents=50_000, unit_cost_cents=30_000, on_hand_qty=0,
                       lead_time_days=0),   # out of stock, no lead time = hard blocker
}


def test_in_stock_is_feasible_now() -> None:
    q = assess_quote_feasibility([QuoteLine("RIM", 10)], _CATALOG, now_ms=_NOW)
    assert q.feasible and q.promised_date_ms == _NOW
    assert q.total_cents == 100_000 and q.blended_margin_pct == 0.4 and not q.approval_requirements


def test_lead_time_is_feasible_later_with_a_constraint() -> None:
    q = assess_quote_feasibility([QuoteLine("HUB", 5)], _CATALOG, now_ms=_NOW)
    assert q.feasible and q.promised_date_ms == _NOW + 7 * _DAY
    assert any("lead time" in c for c in q.constraints)
    # margin 1500/20000 = 7.5% < 20% floor → approval required
    assert any("margin" in a for a in q.approval_requirements)


def test_out_of_stock_no_lead_time_is_a_blocker_with_substitute() -> None:
    q = assess_quote_feasibility([QuoteLine("GEM", 1)], _CATALOG, now_ms=_NOW)
    assert not q.feasible and q.promised_date_ms is None
    assert any("out of stock" in b for b in q.blockers)


def test_unknown_item_blocks() -> None:
    q = assess_quote_feasibility([QuoteLine("NOPE", 1)], _CATALOG, now_ms=_NOW)
    assert not q.feasible and any("unknown item" in b for b in q.blockers)


def test_large_total_and_discount_require_approval_and_reprice() -> None:
    q = assess_quote_feasibility([QuoteLine("RIM", 300)], _CATALOG, now_ms=_NOW,
                                 approval_over_cents=2_000_000, customer_discount_pct=0.1)
    # 300 * 10000 * 0.9 = 2,700,000 > 2,000,000 threshold
    assert q.total_cents == 2_700_000 and any("exceeds approval" in a for a in q.approval_requirements)
    assert any("discount" in c for c in q.constraints)


def test_draft_quote_plan_shape() -> None:
    q = assess_quote_feasibility([QuoteLine("RIM", 2), QuoteLine("HUB", 1)], _CATALOG, now_ms=_NOW)
    plan = q.draft_quote_plan()
    assert plan["currency"] == "USD" and len(plan["lines"]) == 2
    assert plan["lines"][0]["item_ref"] == "RIM" and plan["lines"][0]["line_total_cents"] == 20_000
