"""Product / offer resolution + unit-economics normalization (Market & Funnel Intelligence, Phase 2).

Before any price comparison, two offers must be established as genuinely comparable — otherwise "we're 8%
pricier" is noise comparing a 3-pack to a single unit. This resolves one of OUR products against an external
listing into a typed relationship (exact → not comparable), identifiers dominating model inference, and
normalizes list price + shipping + discount + bundle quantity into delivered **unit** economics so an undercut
is declared on like-for-like. Ambiguity (several comparably-good candidates) yields NO automatic comparison.

Mirrors the discrete-state stance of the generic ``entity_resolution`` resolver (strongest signal first; never a
silent probabilistic match), specialized to product semantics. Pure + deterministic; feed it the candidate
records the caller already has.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, Dict, List, Mapping, Optional, Tuple

import re

from ..integrations.business.contracts import BusinessObject

# GTIN-family identifier keys that share one numeric namespace (a 12-digit UPC == a 13-digit EAN with a lead 0).
_GTIN_KEYS = ("gtin", "upc", "ean", "isbn", "jan")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    return set(_norm(s).split())


class MatchRelationship(str, Enum):
    EXACT = "exact"                      # same identifier → the same product
    EQUIVALENT = "equivalent"            # different listing, like-for-like unit (comparable on price)
    CLOSE_SUBSTITUTE = "close_substitute"
    PARTIAL_SUBSTITUTE = "partial_substitute"
    CATEGORY_ONLY = "category_only"      # same category, not a like-for-like swap
    NOT_COMPARABLE = "not_comparable"    # too little signal, or ambiguous → no automatic comparison

    def comparable_on_price(self) -> bool:
        return self in (MatchRelationship.EXACT, MatchRelationship.EQUIVALENT, MatchRelationship.CLOSE_SUBSTITUTE)


@dataclass(frozen=True)
class ProductAttributes:
    """The comparability fields of a product or listing (NOT its price — see PriceComponents)."""
    ref: str = ""                        # caller's id for this product/listing
    identifiers: Mapping[str, str] = field(default_factory=dict)   # {gtin|upc|ean|mpn|model: value}
    brand: str = ""
    title: str = ""
    category: str = ""
    quantity: float = 1.0                # pack size / count / weight in `unit`
    unit: str = ""                       # count | ml | g | oz | seat | month …
    features: Tuple[str, ...] = ()
    seller_type: str = ""                # first_party | marketplace | reseller
    warranty: str = ""


@dataclass(frozen=True)
class MatchThresholds:
    equivalent: float = 0.85
    close: float = 0.60
    partial: float = 0.40
    category: float = 0.20
    ambiguity_margin: float = 0.08       # 2nd candidate within this of the top → ambiguous


@dataclass(frozen=True)
class ProductMatch(BusinessObject):
    """One of our products resolved to an external listing. ``comparable_on_price`` on the relationship gates
    whether a price delta may even be computed."""
    KIND: ClassVar[str] = "market.product_match"
    our_ref: str = ""
    external_ref: str = ""
    relationship: str = MatchRelationship.NOT_COMPARABLE.value
    attributes_compared: Tuple[str, ...] = ()
    confidence: float = 0.0
    candidate_refs: Tuple[str, ...] = ()   # populated when ambiguous
    evidence: Tuple[str, ...] = ()

    def can_compare_price(self) -> bool:
        return MatchRelationship(self.relationship).comparable_on_price()


def _gtin_norm(v: str) -> str:
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return digits.lstrip("0") or ("0" if digits else "")


def _identifier_match(a: Mapping[str, str], b: Mapping[str, str]) -> "Tuple[bool, str]":
    al = {k.lower(): str(v) for k, v in a.items() if v}
    bl = {k.lower(): str(v) for k, v in b.items() if v}
    # GTIN family: compare across keys in one normalized namespace
    a_gtins = {_gtin_norm(al[k]) for k in _GTIN_KEYS if k in al and _gtin_norm(al[k])}
    b_gtins = {_gtin_norm(bl[k]) for k in _GTIN_KEYS if k in bl and _gtin_norm(bl[k])}
    if a_gtins & b_gtins:
        return True, "gtin"
    for key in ("mpn", "model"):
        if al.get(key) and _norm(al[key]) and _norm(al[key]) == _norm(bl.get(key, "")):
            return True, key
    return False, ""


def _qty_relation(a: ProductAttributes, b: ProductAttributes) -> str:
    """'same' (same unit+quantity), 'diff_qty' (same unit, different quantity), or 'incomparable'."""
    if a.unit and b.unit and _norm(a.unit) == _norm(b.unit):
        if abs(a.quantity - b.quantity) <= 0.01 * max(a.quantity, b.quantity, 1.0):
            return "same"
        return "diff_qty"
    if not a.unit and not b.unit:
        return "same" if abs(a.quantity - b.quantity) <= 1e-9 else "diff_qty"
    return "incomparable"


def resolve_product(ours: ProductAttributes, external: ProductAttributes,
                    *, thresholds: MatchThresholds = MatchThresholds()) -> ProductMatch:
    ev: List[str] = []
    compared: List[str] = []

    matched, how = _identifier_match(ours.identifiers, external.identifiers)
    qty = _qty_relation(ours, external)
    if matched:
        compared.append(how)
        # same identifier but a different pack/count is NOT the same purchasable unit → strong substitute
        if how == "gtin" and qty != "diff_qty":
            return ProductMatch(prov=_prov(), our_ref=ours.ref,
                                external_ref=external.ref, relationship=MatchRelationship.EXACT.value,
                                attributes_compared=tuple(compared), confidence=0.99,
                                evidence=(f"identifier:{how}",))
        ev.append(f"identifier:{how}")

    # attribute scoring (model/brand/category/title/quantity)
    score = 0.0
    if matched and how in ("mpn", "model"):
        score += 0.45; compared.append(how)
    if ours.brand and _norm(ours.brand) == _norm(external.brand):
        score += 0.20; compared.append("brand"); ev.append("brand match")
    if ours.category and _norm(ours.category) == _norm(external.category):
        score += 0.10; compared.append("category")
    ot, et = _tokens(ours.title), _tokens(external.title)
    if ot and et:
        jac = len(ot & et) / len(ot | et)
        if jac > 0:
            score += 0.15 * jac; compared.append("title")
    if qty == "same":
        score += 0.10; compared.append("quantity")
    elif qty == "diff_qty":
        compared.append("quantity")

    if not compared:
        return ProductMatch(prov=_prov(), our_ref=ours.ref, external_ref=external.ref,
                            relationship=MatchRelationship.NOT_COMPARABLE.value, confidence=0.0,
                            evidence=("insufficient attributes",))

    if score >= thresholds.equivalent:
        rel = MatchRelationship.EQUIVALENT
    elif score >= thresholds.close:
        rel = MatchRelationship.CLOSE_SUBSTITUTE
    elif score >= thresholds.partial:
        rel = MatchRelationship.PARTIAL_SUBSTITUTE
    elif score >= thresholds.category:
        rel = MatchRelationship.CATEGORY_ONLY
    else:
        rel = MatchRelationship.NOT_COMPARABLE

    # a different pack/count can never be EQUIVALENT (not the same purchasable unit) — cap at close substitute
    if qty == "diff_qty" and rel is MatchRelationship.EQUIVALENT:
        rel = MatchRelationship.CLOSE_SUBSTITUTE
        ev.append("different pack size → substitute, not equivalent")
    if qty == "incomparable" and rel.comparable_on_price():
        rel = MatchRelationship.PARTIAL_SUBSTITUTE
        ev.append("incomparable units → not price-comparable")

    return ProductMatch(prov=_prov(), our_ref=ours.ref, external_ref=external.ref, relationship=rel.value,
                        attributes_compared=tuple(dict.fromkeys(compared)), confidence=round(score, 4),
                        evidence=tuple(ev))


def resolve_listings(ours: ProductAttributes, candidates: List[ProductAttributes],
                     *, thresholds: MatchThresholds = MatchThresholds()) -> ProductMatch:
    """Resolve our product against several external listings. If two comparable candidates are within the
    ambiguity margin, refuse to pick — NOT_COMPARABLE with the tied candidates — so no automatic comparison runs."""
    scored = sorted((resolve_product(ours, c, thresholds=thresholds) for c in candidates),
                    key=lambda m: m.confidence, reverse=True)
    if not scored:
        return ProductMatch(prov=_prov(), our_ref=ours.ref,
                            relationship=MatchRelationship.NOT_COMPARABLE.value, evidence=("no candidates",))
    best = scored[0]
    contenders = [m for m in scored if m.can_compare_price()
                  and best.confidence - m.confidence <= thresholds.ambiguity_margin and m.confidence > 0]
    if best.can_compare_price() and len(contenders) > 1:
        return ProductMatch(prov=_prov(), our_ref=ours.ref,
                            relationship=MatchRelationship.NOT_COMPARABLE.value, confidence=best.confidence,
                            candidate_refs=tuple(m.external_ref for m in contenders),
                            evidence=("ambiguous: multiple comparable listings — no automatic comparison",))
    return best


# ── unit-economics normalization ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PriceComponents:
    listed_price: float = 0.0
    shipping: float = 0.0
    discount: float = 0.0            # absolute amount off the listed price
    coupon: float = 0.0              # absolute amount off at checkout
    currency: str = ""
    quantity: float = 1.0            # units in the purchasable bundle


@dataclass(frozen=True)
class NormalizedPrice:
    effective_price: float           # listed − discount − coupon (≥ 0)
    delivered_price: float           # effective + shipping
    unit_price: float                # delivered / quantity
    currency: str
    quantity: float


def normalize_price(p: PriceComponents) -> NormalizedPrice:
    """Collapse list price + shipping + discount + coupon + bundle quantity into delivered unit economics —
    the only basis on which an undercut may be declared (plan §4 'normalize unit economics before an undercut')."""
    effective = max(0.0, p.listed_price - p.discount - p.coupon)
    delivered = effective + p.shipping
    qty = p.quantity if p.quantity > 0 else 1.0
    return NormalizedPrice(effective_price=round(effective, 4), delivered_price=round(delivered, 4),
                           unit_price=round(delivered / qty, 4), currency=p.currency, quantity=qty)


@dataclass(frozen=True)
class PricePosition:
    comparable: bool
    relative_premium: Optional[float] = None   # (ours − theirs) / theirs on delivered unit price
    our_unit_price: Optional[float] = None
    their_unit_price: Optional[float] = None
    reason: str = ""


def price_position(match: ProductMatch, ours: PriceComponents, theirs: PriceComponents) -> PricePosition:
    """Relative delivered-unit-price premium — ONLY when the match is price-comparable and the currencies agree.
    Refuses (comparable=False) otherwise, so non-equivalent offers never produce a premium number."""
    if not match.can_compare_price():
        return PricePosition(comparable=False, reason=f"relationship {match.relationship} is not price-comparable")
    if ours.currency and theirs.currency and ours.currency != theirs.currency:
        return PricePosition(comparable=False, reason=f"currency mismatch {ours.currency}≠{theirs.currency}")
    o, t = normalize_price(ours), normalize_price(theirs)
    if t.unit_price <= 0:
        return PricePosition(comparable=False, reason="their unit price is zero/unknown")
    return PricePosition(comparable=True, relative_premium=round((o.unit_price - t.unit_price) / t.unit_price, 4),
                         our_unit_price=o.unit_price, their_unit_price=t.unit_price)


def _prov():
    from ..integrations.business.contracts import Provenance
    return Provenance(provider="market.resolution")
