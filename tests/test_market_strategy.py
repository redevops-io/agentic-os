"""Market & Funnel Intelligence Phase 4 — strategy primitives + survival/weak-signal metrics.

Proves: tactics are extracted from an observation's structured state; a change is classified by its most-material
dimension (a price cut outranks a cosmetic tweak); survival/variant/velocity/reversion are computed over the
Phase-1 history; and cross-competitor frequency counts INDEPENDENT entities (one entity repeating never inflates).
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import (
    MarketChange, MarketObservation, ObservationHistory, StrategyPrimitive, classify_change,
    cross_competitor_strategies, extract_primitives, survival_metrics,
)


def _obs(sref, t, state):
    return MarketObservation(prov=Provenance(provider="web", observed_at=t), surface_ref=sref,
                             observed_at=str(t), source="web", structured_state=state)


# ── primitive extraction ──────────────────────────────────────────────────────────────────────────────────
def test_extract_primitives_from_state():
    o = _obs("s1", 1000, {"pricing_frame": "daily_equivalent", "urgency": "ends tonight",
                          "guarantee": "30-day", "price": "49"})
    prims = {p.type: p.value for p in extract_primitives(o, entity_ref="e1")}
    assert prims["pricing_frame"] == "daily_equivalent" and prims["urgency"] == "ends tonight"
    assert prims["guarantee"] == "30-day" and "price" not in prims    # price is not a tactic primitive
    assert all(isinstance(p, StrategyPrimitive) and p.entity_ref == "e1" for p in extract_primitives(o, entity_ref="e1"))


# ── change significance ───────────────────────────────────────────────────────────────────────────────────
def test_classify_change_picks_most_material_dimension():
    price_and_css = MarketChange(prov=Provenance(provider="w"), surface_ref="s1",
                                 dimensions_changed=("headline", "price"))
    assert classify_change(price_and_css) == "PRICE"       # price outranks positioning/cosmetic
    cosmetic = MarketChange(prov=Provenance(provider="w"), surface_ref="s1", dimensions_changed=("urgency",))
    assert classify_change(cosmetic) == "CONTENT"
    stock = MarketChange(prov=Provenance(provider="w"), surface_ref="s1",
                         dimensions_changed=("stock_state", "discount"))
    assert classify_change(stock) == "AVAILABILITY"        # availability outranks promotion


# ── survival / velocity ───────────────────────────────────────────────────────────────────────────────────
def test_survival_metrics_over_history():
    h = ObservationHistory()
    day = 86_400_000
    h.record(_obs("s1", 0, {"price": "100"}))
    h.record(_obs("s1", day, {"price": "90"}))              # change at day 1
    h.record(_obs("s1", 3 * day, {"price": "90"}))          # stable
    m = survival_metrics(h, "s1", "price", now=5 * day)
    assert m.variant_count == 2 and m.change_count == 1
    assert m.current_survival_ms == 5 * day - day            # current value held since the day-1 change
    assert m.reverted_count == 0
    assert round(m.change_velocity_per_day, 3) == round(1 / 3, 3)   # 1 change over a 3-day span


def test_survival_counts_reversion():
    h = ObservationHistory()
    day = 86_400_000
    for t, p in ((0, "100"), (day, "90"), (2 * day, "100")):
        h.record(_obs("s1", t, {"price": p}))
    m = survival_metrics(h, "s1", "price", now=3 * day)
    assert m.change_count == 2 and m.reverted_count == 1     # the 100→90 drop was reverted


# ── cross-competitor frequency (independent entities only) ────────────────────────────────────────────────
def test_cross_competitor_counts_independent_entities():
    def prim(e, val):
        return StrategyPrimitive(prov=Provenance(provider="w"), type="pricing_frame", value=val, entity_ref=e)
    by_entity = {
        "e1": [prim("e1", "daily_equivalent"), prim("e1", "daily_equivalent")],   # repeats — counts once
        "e2": [prim("e2", "daily_equivalent")],
        "e3": [prim("e3", "annual_discount")],
    }
    res = cross_competitor_strategies(by_entity, min_entities=2)
    assert len(res) == 1                                     # only daily_equivalent crosses 2 entities
    top = res[0]
    assert top.value == "daily_equivalent" and top.independent_entity_count == 2
    assert set(top.entity_refs) == {"e1", "e2"}
