"""Market & Funnel Intelligence Phase 1 — persistent immutable observation history.

Proves: a surface's state is reconstructable at any past time; change detection over consecutive observations
yields dimensions_changed + magnitude; persistence and reversion are computed over the series; and offer
observations diff through the same path. The history is never overwritten — each observation is appended.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import (
    MarketChange, MarketEntity, MarketObservation, MarketSurface, ObservationHistory, OfferObservation,
    diff_state,
)


def _surface():
    e = MarketEntity(prov=Provenance(provider="seed"), entity_type="competitor", canonical_name="Globex",
                     domains=("globex.example",))
    s = MarketSurface(prov=Provenance(provider="seed"), entity_ref=e.entity_id(),
                      surface_type="pricing_page", url_or_ref="https://globex.example/pricing")
    return e, s


def _obs(sref, t, state):
    return MarketObservation(prov=Provenance(provider="web", observed_at=t), surface_ref=sref,
                             observed_at=str(t), source="web", structured_state=state)


# ── identity ──────────────────────────────────────────────────────────────────────────────────────────────
def test_entity_and_surface_ids_are_stable_content_addresses():
    e1 = MarketEntity(prov=Provenance(provider="a"), canonical_name="X", domains=("x.io",))
    e2 = MarketEntity(prov=Provenance(provider="b"), canonical_name="X", domains=("x.io",))
    assert e1.entity_id() == e2.entity_id()                 # same facts, different provenance → same id
    s = MarketSurface(prov=Provenance(provider="a"), entity_ref=e1.entity_id(), surface_type="landing_page")
    assert s.surface_id().startswith("sha256:")


# ── reconstruction ────────────────────────────────────────────────────────────────────────────────────────
def test_history_is_reconstructable_at_any_past_time():
    _e, s = _surface()
    sref = s.surface_id()
    hist = ObservationHistory()
    hist.record(_obs(sref, 1000, {"price": "100", "headline": "Save big"}))
    hist.record(_obs(sref, 2000, {"price": "90", "headline": "Save big"}))
    hist.record(_obs(sref, 3000, {"price": "100", "headline": "New year"}))

    assert len(hist.history(sref)) == 3
    assert hist.at(sref, 2500).structured_state["price"] == "90"      # state in effect at t=2500
    assert hist.at(sref, 500) is None                                 # before any observation
    assert hist.latest(sref).structured_state["headline"] == "New year"


def test_append_only_never_overwrites():
    _e, s = _surface()
    sref = s.surface_id()
    hist = ObservationHistory()
    hist.record(_obs(sref, 1000, {"price": "100"}))
    hist.record(_obs(sref, 2000, {"price": "90"}))     # same surface, new point-in-time — not an overwrite
    prices = [o.structured_state["price"] for o in hist.history(sref)]
    assert prices == ["100", "90"]                     # both retained, in observed order


# ── change detection + persistence + reversion ────────────────────────────────────────────────────────────
def test_changes_detect_persistence_and_reversion():
    _e, s = _surface()
    sref = s.surface_id()
    hist = ObservationHistory()
    hist.record(_obs(sref, 1000, {"price": "100"}))
    hist.record(_obs(sref, 2000, {"price": "90"}))     # drop
    hist.record(_obs(sref, 3000, {"price": "100"}))    # back up (reverts the drop)

    changes = hist.changes(sref, now=5000)
    assert len(changes) == 2
    drop, restore = changes
    assert drop.dimensions_changed == ("price",) and abs(drop.magnitude - 0.1) < 1e-9
    assert drop.persisted_for_ms == 1000 and drop.reverted is True    # held 1s then reverted
    assert restore.persisted_for_ms == 2000 and restore.reverted is False   # held until now (5000-3000)
    assert isinstance(drop, MarketChange) and drop.before_ref.startswith("sha256:")


def test_no_change_when_state_is_stable():
    _e, s = _surface()
    sref = s.surface_id()
    hist = ObservationHistory()
    hist.record(_obs(sref, 1000, {"price": "100"}))
    hist.record(_obs(sref, 2000, {"price": "100"}))    # identical → no change emitted
    assert hist.changes(sref, now=3000) == []


# ── offer observations diff through the same path ─────────────────────────────────────────────────────────
def test_offer_observations_change_detection():
    _e, s = _surface()
    sref = s.surface_id()
    hist = ObservationHistory()
    hist.record(OfferObservation(prov=Provenance(provider="mp", observed_at=1000), surface_ref=sref,
                                 observed_at="1000", effective_price="18.90", stock_state="in_stock"))
    hist.record(OfferObservation(prov=Provenance(provider="mp", observed_at=2000), surface_ref=sref,
                                 observed_at="2000", effective_price="17.70", stock_state="in_stock"))
    changes = hist.changes(sref, kind=OfferObservation.KIND, now=3000)
    assert len(changes) == 1 and "effective_price" in changes[0].dimensions_changed
    assert abs(changes[0].magnitude - (1.20 / 18.90)) < 1e-6


# ── diff primitive ────────────────────────────────────────────────────────────────────────────────────────
def test_diff_state_numeric_and_non_numeric():
    dims, mag = diff_state({"price": "100"}, {"price": "80"})
    assert dims == ("price",) and abs(mag - 0.2) < 1e-9
    dims2, mag2 = diff_state({"cta": "Buy"}, {"cta": "Start free trial"})
    assert dims2 == ("cta",) and mag2 == 1.0            # non-numeric change → flat magnitude
    assert diff_state({"a": "1"}, {"a": "1"}) == ((), 0.0)
