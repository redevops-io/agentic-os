"""Market & Funnel Intelligence Phase 2 — product/offer resolution + unit-economics normalization.

Proves: exact identifier → EXACT; same model different pack → not exact (close substitute); bundles normalized
by unit; ambiguity → no automatic comparison; and a price premium is only produced for genuinely comparable,
same-currency offers on delivered unit price (the exit criterion: comparison never mixes non-equivalent offers).
"""
from __future__ import annotations

from agentic_os.market import (
    MatchRelationship, PriceComponents, ProductAttributes, normalize_price, price_position, resolve_listings,
    resolve_product,
)


def _p(ref, **kw):
    return ProductAttributes(ref=ref, **kw)


# ── resolution ladder ─────────────────────────────────────────────────────────────────────────────────────
def test_exact_identifier_is_exact():
    ours = _p("sku1", identifiers={"upc": "012345678905"}, brand="Acme", title="Widget", quantity=1, unit="count")
    ext = _p("L1", identifiers={"ean": "0012345678905"}, brand="Acme", title="Widget 1ct", quantity=1, unit="count")
    m = resolve_product(ours, ext)
    assert m.relationship == MatchRelationship.EXACT.value and m.confidence >= 0.99 and m.can_compare_price()


def test_same_model_different_count_is_not_exact():
    ours = _p("sku1", identifiers={"model": "WX-10"}, brand="Acme", title="Widget", quantity=1, unit="count")
    ext = _p("L1", identifiers={"model": "WX-10"}, brand="Acme", title="Widget 3-pack", quantity=3, unit="count")
    m = resolve_product(ours, ext)
    assert m.relationship != MatchRelationship.EXACT.value
    assert m.relationship == MatchRelationship.CLOSE_SUBSTITUTE.value   # same model, different pack
    assert "quantity" in m.attributes_compared and m.can_compare_price()


def test_insufficient_attributes_is_not_comparable():
    m = resolve_product(_p("sku1"), _p("L1"))
    assert m.relationship == MatchRelationship.NOT_COMPARABLE.value and not m.can_compare_price()


def test_category_only_when_weak_signal():
    ours = _p("sku1", category="supplements", title="Vitamin C 1000mg", quantity=60, unit="count")
    ext = _p("L1", category="supplements", title="Fish Oil Omega 3", quantity=120, unit="count")
    m = resolve_product(ours, ext)
    assert m.relationship in (MatchRelationship.CATEGORY_ONLY.value, MatchRelationship.NOT_COMPARABLE.value)


def test_ambiguous_listings_block_automatic_comparison():
    ours = _p("sku1", identifiers={"model": "WX-10"}, brand="Acme", title="Acme Widget", quantity=1, unit="count")
    a = _p("La", identifiers={"model": "WX-10"}, brand="Acme", title="Acme Widget", quantity=1, unit="count")
    b = _p("Lb", identifiers={"model": "WX-10"}, brand="Acme", title="Acme Widget", quantity=1, unit="count")
    m = resolve_listings(ours, [a, b])
    assert m.relationship == MatchRelationship.NOT_COMPARABLE.value
    assert set(m.candidate_refs) == {"La", "Lb"} and "ambiguous" in m.evidence[0]


def test_resolve_listings_picks_best_when_unambiguous():
    ours = _p("sku1", identifiers={"upc": "012345678905"}, brand="Acme", title="Widget", quantity=1, unit="count")
    good = _p("Lg", identifiers={"upc": "012345678905"}, brand="Acme", title="Widget", quantity=1, unit="count")
    weak = _p("Lw", category="widgets", title="Other thing", quantity=1, unit="count")
    m = resolve_listings(ours, [weak, good])
    assert m.external_ref == "Lg" and m.relationship == MatchRelationship.EXACT.value


# ── unit-economics normalization ──────────────────────────────────────────────────────────────────────────
def test_normalize_delivered_unit_price_with_bundle_and_shipping():
    n = normalize_price(PriceComponents(listed_price=30.0, shipping=5.0, discount=2.0, coupon=3.0,
                                        currency="usd", quantity=5))
    assert n.effective_price == 25.0 and n.delivered_price == 30.0 and n.unit_price == 6.0


def test_price_position_only_for_comparable_same_currency():
    ours = _p("sku1", identifiers={"upc": "012345678905"}, brand="Acme", title="Widget", quantity=1, unit="count")
    ext = _p("L1", identifiers={"upc": "012345678905"}, brand="Acme", title="Widget", quantity=1, unit="count")
    match = resolve_product(ours, ext)
    # our delivered unit $18.90 vs their $17.70 → +6.8% premium
    pos = price_position(match, PriceComponents(listed_price=16.90, shipping=2.0, currency="usd", quantity=1),
                         PriceComponents(listed_price=15.70, shipping=2.0, currency="usd", quantity=1))
    assert pos.comparable and abs(pos.relative_premium - ((18.90 - 17.70) / 17.70)) < 1e-3   # 4-dp rounded


def test_price_position_refuses_non_comparable_and_currency_mismatch():
    weak = resolve_product(_p("sku1", category="x", title="A A", quantity=1), _p("L1", category="y", title="B B", quantity=1))
    assert not price_position(weak, PriceComponents(listed_price=10, currency="usd"),
                              PriceComponents(listed_price=9, currency="usd")).comparable
    ours = _p("sku1", identifiers={"upc": "012345678905"}, title="W", quantity=1, unit="count")
    ext = _p("L1", identifiers={"upc": "012345678905"}, title="W", quantity=1, unit="count")
    ok_match = resolve_product(ours, ext)
    pos = price_position(ok_match, PriceComponents(listed_price=10, currency="usd"),
                         PriceComponents(listed_price=9, currency="eur"))
    assert not pos.comparable and "currency" in pos.reason
