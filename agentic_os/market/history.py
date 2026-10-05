"""Persistent market model — immutable observation history (Market & Funnel Intelligence, Phase 1).

The existing market pipeline is stateless/tick-based: it detects patterns in a bundle of observations but keeps
no longitudinal record, so it can see *that* a competitor page changed (``PageSnapshot.content_hash``) but not
*when it changed, how long the change persisted, or whether it reverted* — and persistence/reversion are the
signal (plan §6). This adds the foundation the rest of the program stands on:

    MarketEntity (who)  →  MarketSurface (what/where)  →  MarketObservation (immutable point-in-time state)
                                                           → ObservationHistory (append-only) → MarketChange

Two rules the plan insists on: **never overwrite yesterday's state — the history IS the product**, and an
observation is a point-in-time *fact* (``observed_at``) distinct from when we learned it (``known_at``). Offer
and funnel specializations carry typed economics / funnel fields but store the same way, so one surface's full
history — generic state, prices, funnel — is reconstructable at any past time. Reuses the Integration-plane
``BusinessObject`` so every record is content-addressed and replayable like every other canonical object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Tuple

from ..integrations.business.contracts import BusinessObject, now_ms

ENTITY_TYPES = ("competitor", "brand", "seller", "product", "service")
SURFACE_TYPES = ("marketplace_listing", "product_page", "pricing_page", "landing_page", "advertisement",
                 "social_post", "app_store_listing", "signup_flow", "checkout", "booking_flow")


# ── who / what / where ────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MarketEntity(BusinessObject):
    """A market actor we observe — generalizes ``TrackedCompany`` beyond competitors to sellers/products/
    services so a marketplace listing and a SaaS rival share one model."""
    KIND: ClassVar[str] = "market.entity"
    entity_type: str = "competitor"    # competitor | brand | seller | product | service
    canonical_name: str = ""
    domains: Tuple[str, ...] = ()
    marketplace_ids: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()
    geography: str = ""
    category: str = ""

    def entity_id(self) -> str:
        return self.digest()


@dataclass(frozen=True)
class MarketSurface(BusinessObject):
    """A specific observable surface of an entity (a listing, a pricing page, an ad). The stable ``surface_id``
    is what observations and changes are keyed on."""
    KIND: ClassVar[str] = "market.surface"
    entity_ref: str = ""               # MarketEntity.entity_id()
    surface_type: str = "product_page"
    url_or_ref: str = ""
    provider: str = ""                 # observation provider / marketplace
    geography: str = ""
    audience: str = ""

    def surface_id(self) -> str:
        return self.digest()


# ── immutable point-in-time observations ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MarketObservation(BusinessObject):
    """Immutable state of a surface at ``observed_at``. ``structured_state`` is the normalized snapshot
    (headline, price, stock, cta, offer_kind, rank, rating, …) that change detection diffs; raw payloads stay
    in ``prov.evidence_refs``. Never mutated — a new observation is appended."""
    KIND: ClassVar[str] = "market.observation"
    surface_ref: str = ""
    observed_at: str = ""              # ISO datetime (human); prov.observed_at carries the ms epoch
    source: str = ""
    structured_state: Dict[str, str] = field(default_factory=dict)
    extraction_version: str = "1"
    confidence: float = 1.0


@dataclass(frozen=True)
class OfferObservation(BusinessObject):
    """A priced offer observed on a surface at a point in time — the economics change detection needs beyond a
    generic snapshot (effective/unit price, shipping, stock, promotion, rank). Values are strings as observed;
    normalization to comparable unit economics is Phase 2."""
    KIND: ClassVar[str] = "market.offer_observation"
    surface_ref: str = ""
    observed_at: str = ""
    product_or_service: str = ""
    seller: str = ""
    listed_price: str = ""
    effective_price: str = ""
    unit_price: str = ""
    currency: str = ""
    shipping: str = ""
    discount: str = ""
    coupon: str = ""
    bundle: str = ""
    subscription_terms: str = ""
    stock_state: str = ""
    delivery_promise: str = ""
    ranking_or_position: str = ""
    rating: str = ""
    review_count: str = ""
    promotion: str = ""
    confidence: float = 1.0

    def as_state(self) -> Dict[str, str]:
        """Project to the generic structured_state shape so change detection treats offers uniformly."""
        return {k: str(v) for k, v in self.business_fields().items()
                if k not in ("surface_ref", "observed_at", "confidence") and v not in ("", None)}


@dataclass(frozen=True)
class FunnelObservation(BusinessObject):
    """A competitor funnel stage observed at a point in time (headline/positioning/CTA/offer/pricing_frame/
    proof/friction/trial/guarantee/urgency). Versioned over time like any other observation."""
    KIND: ClassVar[str] = "market.funnel_observation"
    surface_ref: str = ""
    observed_at: str = ""
    competitor: str = ""
    stage: str = ""                    # creative | landing | quiz | form | offer | signup | followup
    headline: str = ""
    positioning: str = ""
    cta: str = ""
    offer: str = ""
    pricing_frame: str = ""
    proof: str = ""
    friction: str = ""
    form_fields: Tuple[str, ...] = ()
    trial: str = ""
    guarantee: str = ""
    urgency: str = ""
    creative_refs: Tuple[str, ...] = ()
    confidence: float = 1.0

    def as_state(self) -> Dict[str, str]:
        return {k: str(v) for k, v in self.business_fields().items()
                if k not in ("surface_ref", "observed_at", "confidence") and v not in ("", None, ())}


# ── change ────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MarketChange(BusinessObject):
    """A detected change between two consecutive observations of one surface. ``persisted_for_ms`` is how long
    the after-state held (until the next change, or until ``now``); ``reverted`` means a later observation put a
    changed dimension back to its before-value — both are first-class signal (plan §6)."""
    KIND: ClassVar[str] = "market.change"
    surface_ref: str = ""
    before_ref: str = ""               # MarketObservation.digest() before
    after_ref: str = ""                # MarketObservation.digest() after
    dimensions_changed: Tuple[str, ...] = ()
    magnitude: float = 0.0             # max relative change over numeric dims, else 1.0
    first_seen: str = ""               # the after observation's observed_at
    persisted_for_ms: int = 0
    reverted: bool = False
    confidence: float = 1.0


def _num(v: str) -> Optional[float]:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def diff_state(before: Dict[str, str], after: Dict[str, str]) -> "Tuple[Tuple[str, ...], float]":
    """Return (dimensions_changed, magnitude). Magnitude = max relative change over dims whose before/after both
    parse as numbers (with a non-zero base); 1.0 if any non-numeric dimension changed; 0.0 if nothing changed."""
    keys = set(before) | set(after)
    changed = tuple(sorted(k for k in keys if before.get(k, "") != after.get(k, "")))
    if not changed:
        return (), 0.0
    rel, any_non_numeric = 0.0, False
    for k in changed:
        a, b = _num(after.get(k, "")), _num(before.get(k, ""))
        if a is not None and b not in (None, 0.0):
            rel = max(rel, abs(a - b) / abs(b))
        else:
            any_non_numeric = True
    return changed, (rel if rel > 0 else (1.0 if any_non_numeric else 0.0))


class ObservationHistory:
    """Append-only store of observations per surface (in-memory, with the same persistence seam as the other
    plane stores). The point: reconstruct a surface's state at any past time, and derive the change series —
    including how long each change persisted and whether it reverted."""

    def __init__(self) -> None:
        self._by_surface: Dict[str, List[BusinessObject]] = {}

    # ingestion
    def record(self, obs: BusinessObject) -> BusinessObject:
        sref = getattr(obs, "surface_ref", "")
        self._by_surface.setdefault(sref, []).append(obs)
        return obs

    # reconstruction
    def history(self, surface_ref: str, *, kind: Optional[str] = None) -> List[BusinessObject]:
        rows = [o for o in self._by_surface.get(surface_ref, []) if kind is None or o.KIND == kind]
        return sorted(rows, key=lambda o: o.prov.observed_at)

    def latest(self, surface_ref: str, *, kind: Optional[str] = None) -> Optional[BusinessObject]:
        h = self.history(surface_ref, kind=kind)
        return h[-1] if h else None

    def at(self, surface_ref: str, when_ms: int, *, kind: Optional[str] = None) -> Optional[BusinessObject]:
        """The observation in effect at ``when_ms`` — the most recent one with observed_at <= when_ms."""
        prior = [o for o in self.history(surface_ref, kind=kind) if o.prov.observed_at <= when_ms]
        return prior[-1] if prior else None

    # change detection
    @staticmethod
    def _state_of(o: BusinessObject) -> Dict[str, str]:
        if hasattr(o, "as_state"):
            return o.as_state()           # Offer/Funnel observations
        return dict(getattr(o, "structured_state", {}) or {})

    def changes(self, surface_ref: str, *, kind: str = MarketObservation.KIND,
                now: Optional[int] = None) -> List[MarketChange]:
        """Derive the change series for a surface from its consecutive observations of one kind."""
        series = self.history(surface_ref, kind=kind)
        now = now if now is not None else now_ms()
        out: List[MarketChange] = []
        states = [self._state_of(o) for o in series]
        for i in range(1, len(series)):
            dims, mag = diff_state(states[i - 1], states[i])
            if not dims:
                continue
            nxt = series[i + 1].prov.observed_at if i + 1 < len(series) else now
            persisted = max(0, nxt - series[i].prov.observed_at)
            # reverted iff some later observation returns a changed dim to its before-value
            reverted = any(any(states[j].get(d, "") == states[i - 1].get(d, "") for d in dims)
                           for j in range(i + 1, len(series)))
            out.append(MarketChange(
                prov=series[i].prov, surface_ref=surface_ref,
                before_ref=series[i - 1].digest(), after_ref=series[i].digest(),
                dimensions_changed=dims, magnitude=mag,
                first_seen=getattr(series[i], "observed_at", ""), persisted_for_ms=persisted,
                reverted=reverted, confidence=min(getattr(series[i - 1], "confidence", 1.0),
                                                  getattr(series[i], "confidence", 1.0))))
        return out
