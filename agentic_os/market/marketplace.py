"""Marketplace observation provider + relative price position + recommend-only response (Phase 3).

The first integrated loop: a provider-neutral observation seam (so marketplace APIs / crawlers / feeds are
swappable behind one contract), relative-position features over genuinely-comparable offers (Phase 2 gates
comparability), and a recommend-only decision that joins the market evidence to OUR context (margin floor,
inventory, recent conversion) — never "be cheapest", and never a write. The candidate taxonomy keeps NO_CHANGE
and WATCH as first-class outcomes, and a suggested test price can never cross the contribution-margin floor.

    discover surfaces → observe (→ immutable OfferObservation history) → resolve comparables → relative position
      → recommend (NO_CHANGE | WATCH | INVESTIGATE | PRICE_TEST), recommend-only
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from .history import MarketSurface, OfferObservation
from .resolution import (
    MatchRelationship, PriceComponents, ProductAttributes, normalize_price, price_position, resolve_product,
)


# ── provider-neutral observation seam ─────────────────────────────────────────────────────────────────────
@runtime_checkable
class MarketObservationProvider(Protocol):
    """A source of market observations (a marketplace API, a crawler, a feed). The intelligence logic depends
    on this contract, never a vendor endpoint — so the provider is swappable (plan §13)."""
    provider: str

    def capabilities(self) -> Tuple[str, ...]: ...
    def discover_surfaces(self, entity_ref: str) -> List[MarketSurface]: ...
    def observe(self, surface: MarketSurface) -> Optional[OfferObservation]: ...
    def fetch_history(self, surface_ref: str) -> List[OfferObservation]: ...


class InMemoryMarketObservationProvider:
    """Reference provider over seeded data — lets the price MVP + tests run offline without a live marketplace."""
    provider = "inmemory"

    def __init__(self) -> None:
        self._surfaces: dict[str, List[MarketSurface]] = {}
        self._obs: dict[str, List[OfferObservation]] = {}

    def seed_surface(self, surface: MarketSurface, observations: Sequence[OfferObservation]) -> None:
        self._surfaces.setdefault(surface.entity_ref, []).append(surface)
        self._obs[surface.surface_id()] = list(observations)

    def capabilities(self) -> Tuple[str, ...]:
        return ("offer.read", "offer.history")

    def discover_surfaces(self, entity_ref: str) -> List[MarketSurface]:
        return list(self._surfaces.get(entity_ref, []))

    def observe(self, surface: MarketSurface) -> Optional[OfferObservation]:
        h = self._obs.get(surface.surface_id(), [])
        return h[-1] if h else None

    def fetch_history(self, surface_ref: str) -> List[OfferObservation]:
        return list(self._obs.get(surface_ref, []))


# ── relative position (only over comparable offers) ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class RelativePosition:
    our_unit_price: float
    comparable_count: int
    median_unit_price: float
    leader_unit_price: float             # the lowest comparable delivered unit price
    premium_to_median: float             # (ours − median) / median
    premium_to_leader: float
    rank: int                            # 1 = cheapest among (ours + comparables)
    percentile: float                    # 0 = cheapest, 1 = priciest


def relative_position(our_unit: float, comparable_units: Sequence[float]) -> Optional[RelativePosition]:
    """Our delivered-unit-price position among comparable offers. None if there are no comparables."""
    comps = [u for u in comparable_units if u > 0]
    if not comps or our_unit <= 0:
        return None
    median = statistics.median(comps)
    leader = min(comps)
    everyone = sorted(comps + [our_unit])
    rank = everyone.index(our_unit) + 1
    cheaper_or_eq = sum(1 for u in comps if u <= our_unit)
    return RelativePosition(
        our_unit_price=round(our_unit, 4), comparable_count=len(comps),
        median_unit_price=round(median, 4), leader_unit_price=round(leader, 4),
        premium_to_median=round((our_unit - median) / median, 4),
        premium_to_leader=round((our_unit - leader) / leader, 4),
        rank=rank, percentile=round(cheaper_or_eq / len(comps), 4))


# ── recommend-only price response ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PricingContext:
    """OUR first-party context the decision joins to — the point is our outcomes, not competitors' prices."""
    unit_cost: float = 0.0               # our delivered cost per unit (for the margin floor)
    min_margin: float = 0.30             # contribution-margin floor; a test price may never cross it
    inventory_days: Optional[float] = None   # days of cover; high + aging strengthens a markdown
    conversion_delta: Optional[float] = None  # recent change in OUR conversion (−0.1 = down 10%)
    premium_threshold: float = 0.05      # a premium above this is "material"


@dataclass(frozen=True)
class CompetitorOffer:
    attributes: ProductAttributes
    price: PriceComponents


@dataclass(frozen=True)
class PriceRecommendation:
    action: str                          # NO_CHANGE | WATCH | INVESTIGATE | PRICE_TEST
    rationale: str
    position: Optional[RelativePosition] = None
    suggested_unit_price: Optional[float] = None   # set only for PRICE_TEST; always ≥ margin floor
    margin_floor_unit_price: Optional[float] = None
    comparable_count: int = 0
    evidence: Tuple[str, ...] = ()


def _floor_price(ctx: PricingContext) -> Optional[float]:
    if ctx.unit_cost <= 0 or not (0.0 <= ctx.min_margin < 1.0):
        return None
    return round(ctx.unit_cost / (1.0 - ctx.min_margin), 4)


def recommend_price_response(ours_attrs: ProductAttributes, ours_price: PriceComponents,
                             competitors: Sequence[CompetitorOffer], ctx: PricingContext) -> PriceRecommendation:
    """Recommend-only (plan §9/§10). Joins relative position to OUR margin/inventory/conversion; NO_CHANGE and
    WATCH are first-class; a PRICE_TEST suggestion is clamped to the contribution-margin floor and never below."""
    comparable_units: List[float] = []
    ev: List[str] = []
    for c in competitors:
        m = resolve_product(ours_attrs, c.attributes)
        if m.can_compare_price():
            pos = price_position(m, ours_price, c.price)
            if pos.comparable and pos.their_unit_price:
                comparable_units.append(pos.their_unit_price)
        else:
            ev.append(f"skipped {c.attributes.ref or '?'}: {m.relationship}")

    if not comparable_units:
        return PriceRecommendation(action="INVESTIGATE", rationale="no genuinely comparable offers to price against",
                                   comparable_count=0, evidence=tuple(ev) or ("no comparables",))

    our_unit = normalize_price(ours_price).unit_price
    pos = relative_position(our_unit, comparable_units)
    floor = _floor_price(ctx)
    base = PriceRecommendation(action="", rationale="", position=pos, comparable_count=pos.comparable_count,
                               margin_floor_unit_price=floor, evidence=tuple(ev))

    if pos.premium_to_median <= ctx.premium_threshold:
        return _with(base, "NO_CHANGE", f"at/below the comparable median (premium {pos.premium_to_median:+.1%})")

    # material premium — join to OUR signals
    conv_down = ctx.conversion_delta is not None and ctx.conversion_delta <= -0.05
    inv_high = ctx.inventory_days is not None and ctx.inventory_days >= 60
    if not conv_down and not inv_high:
        return _with(base, "WATCH", f"premium {pos.premium_to_median:+.1%} but our conversion/inventory are healthy")

    # a test toward the median, floored by contribution margin
    target = pos.median_unit_price
    if floor is not None and target < floor:
        if our_unit <= floor:
            return _with(base, "WATCH",
                         f"premium {pos.premium_to_median:+.1%} but the median is below our margin floor — no room")
        target = floor
    reason = (f"premium {pos.premium_to_median:+.1%} vs median with " +
              ("conversion down " if conv_down else "") + ("inventory high" if inv_high else "")).strip()
    note = (f"target {target:.2f} ≥ floor {floor}",) if floor is not None else ()
    return PriceRecommendation(action="PRICE_TEST", rationale=reason, position=pos,
                               suggested_unit_price=round(target, 4), margin_floor_unit_price=floor,
                               comparable_count=pos.comparable_count, evidence=tuple(ev) + note)


def _with(base: PriceRecommendation, action: str, rationale: str) -> PriceRecommendation:
    return PriceRecommendation(action=action, rationale=rationale, position=base.position,
                               margin_floor_unit_price=base.margin_floor_unit_price,
                               comparable_count=base.comparable_count, evidence=base.evidence)
