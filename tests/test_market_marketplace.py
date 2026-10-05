"""Market & Funnel Intelligence Phase 3 — marketplace provider + relative position + recommend-only response.

Proves: the observation provider is swappable behind one contract and feeds immutable offer history; relative
position is computed ONLY over genuinely-comparable offers; and the recommend-only decision joins position to
OUR margin/inventory/conversion — NO_CHANGE and WATCH are first-class, a PRICE_TEST never crosses the margin
floor, and nothing executes.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import (
    CompetitorOffer, InMemoryMarketObservationProvider, MarketObservationProvider, MarketSurface,
    OfferObservation, PriceComponents, PricingContext, ProductAttributes, recommend_price_response,
    relative_position,
)


def _attrs(ref, **kw):
    return ProductAttributes(ref=ref, identifiers={"upc": "012345678905"}, brand="Acme", title="Widget",
                             quantity=1, unit="count", **kw)


def _comp(ref, listed, shipping=0.0):
    return CompetitorOffer(attributes=_attrs(ref), price=PriceComponents(listed_price=listed, shipping=shipping,
                                                                         currency="usd", quantity=1))


# ── provider seam ─────────────────────────────────────────────────────────────────────────────────────────
def test_inmemory_provider_satisfies_contract_and_feeds_history():
    p = InMemoryMarketObservationProvider()
    assert isinstance(p, MarketObservationProvider)
    s = MarketSurface(prov=Provenance(provider="mp"), entity_ref="e1", surface_type="marketplace_listing")
    obs = [OfferObservation(prov=Provenance(provider="mp", observed_at=t), surface_ref=s.surface_id(),
                            observed_at=str(t), effective_price=str(p_)) for t, p_ in ((1000, "19.0"), (2000, "18.0"))]
    p.seed_surface(s, obs)
    assert p.discover_surfaces("e1")[0].surface_type == "marketplace_listing"
    assert p.observe(s).effective_price == "18.0"                      # latest
    assert len(p.fetch_history(s.surface_id())) == 2 and "offer.history" in p.capabilities()


# ── relative position ─────────────────────────────────────────────────────────────────────────────────────
def test_relative_position_math():
    pos = relative_position(18.90, [17.70, 17.50, 18.00])
    assert pos.comparable_count == 3 and pos.median_unit_price == 17.70 and pos.leader_unit_price == 17.50
    assert abs(pos.premium_to_median - (18.90 - 17.70) / 17.70) < 1e-3
    assert pos.rank == 4 and pos.percentile == 1.0                      # priciest
    assert relative_position(10.0, []) is None


# ── recommend-only decisions ──────────────────────────────────────────────────────────────────────────────
def _ours_price(unit):
    return PriceComponents(listed_price=unit, currency="usd", quantity=1)


def test_no_change_when_at_or_below_median():
    rec = recommend_price_response(_attrs("ours"), _ours_price(17.50),
                                   [_comp("c1", 17.70), _comp("c2", 18.00)],
                                   PricingContext(unit_cost=10.0, min_margin=0.3))
    assert rec.action == "NO_CHANGE" and rec.comparable_count == 2


def test_watch_when_premium_but_healthy():
    rec = recommend_price_response(_attrs("ours"), _ours_price(20.0),
                                   [_comp("c1", 17.0), _comp("c2", 18.0)],
                                   PricingContext(unit_cost=10.0, min_margin=0.3, conversion_delta=0.0,
                                                  inventory_days=20))
    assert rec.action == "WATCH" and rec.position.premium_to_median > 0.05


def test_price_test_when_premium_and_conversion_down_respects_floor():
    # median 17.5, our 20.0, conversion down → PRICE_TEST toward median, but floor = 15/(1-.3)=21.43 forces WATCH
    low_floor = recommend_price_response(_attrs("ours"), _ours_price(20.0),
                                         [_comp("c1", 17.0), _comp("c2", 18.0)],
                                         PricingContext(unit_cost=10.0, min_margin=0.3, conversion_delta=-0.12))
    assert low_floor.action == "PRICE_TEST" and low_floor.suggested_unit_price == 17.5
    assert low_floor.suggested_unit_price >= low_floor.margin_floor_unit_price    # 14.29 floor

    high_floor = recommend_price_response(_attrs("ours"), _ours_price(20.0),
                                          [_comp("c1", 17.0), _comp("c2", 18.0)],
                                          PricingContext(unit_cost=15.0, min_margin=0.3, conversion_delta=-0.12))
    # floor = 15/0.7 = 21.43 > median 17.5 AND > our 20.0 → no room → WATCH, nothing below floor suggested
    assert high_floor.action == "WATCH" and high_floor.suggested_unit_price is None


def test_investigate_when_no_comparable_offers():
    # competitor is a non-comparable product (different category, no shared identifiers)
    other = CompetitorOffer(attributes=ProductAttributes(ref="x", category="other", title="Totally Different",
                                                         quantity=1, unit="count"),
                            price=PriceComponents(listed_price=5.0, currency="usd", quantity=1))
    rec = recommend_price_response(_attrs("ours"), _ours_price(20.0), [other],
                                   PricingContext(unit_cost=10.0, min_margin=0.3))
    assert rec.action == "INVESTIGATE" and rec.comparable_count == 0
