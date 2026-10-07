"""Offer Decisioning — choose an eligible offer under policy (plan §13, P4).

Generalizes the "shopping assistant" concept without tying it to retail: given a subject (visitor/segment/account),
eligible offers, and hard policy constraints, pick the offer that maximizes expected value WITHOUT violating a hard
constraint (margin floor, allowed kinds, capacity, discount ceiling). Pricing/contract authority is never averaged
or majority-voted — a feasible offer or NO offer. Integrates with Funnel (CHANGE_OFFER) and Quoting rather than
being a standalone agent. Pure + deterministic + explainable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Offer:
    """A candidate offer. ``discount`` is a fraction 0..1; ``margin`` is the resulting fraction 0..1."""
    offer_id: str
    kind: str = ""                 # discount | bundle | trial_extension | upsell | renewal | freemium | shipping | ...
    value: float = 0.0            # nominal value to the business if taken
    customer_value: float = 0.0   # value to the customer (for ranking / fairness)
    margin: float = 0.0
    discount: float = 0.0
    requires_inventory: bool = False
    description: str = ""


@dataclass(frozen=True)
class OfferConstraints:
    """Hard policy constraints. An offer that violates ANY of these is ineligible (never relaxed)."""
    max_discount: float = 1.0
    min_margin: float = 0.0
    allowed_kinds: Tuple[str, ...] = ()        # empty = any kind allowed
    inventory_available: bool = True
    policy: str = ""


@dataclass(frozen=True)
class OfferDecision:
    """The selected offer + why (§13). ``selected_offer`` is None when nothing satisfies policy (a valid NO_ACTION)."""
    subject: str
    eligible_offers: Tuple[Offer, ...] = ()
    feasible_offers: Tuple[Offer, ...] = ()
    selected_offer: Optional[Offer] = None
    expected_value: float = 0.0
    customer_value: float = 0.0
    margin: float = 0.0
    policy: str = ""
    explanation: str = ""

    @property
    def is_no_action(self) -> bool:
        return self.selected_offer is None


def _violations(offer: Offer, c: OfferConstraints) -> Tuple[str, ...]:
    v = []
    if offer.discount > c.max_discount:
        v.append(f"discount {offer.discount} > max {c.max_discount}")
    if offer.margin < c.min_margin:
        v.append(f"margin {offer.margin} < min {c.min_margin}")
    if c.allowed_kinds and offer.kind not in c.allowed_kinds:
        v.append(f"kind {offer.kind!r} not allowed")
    if offer.requires_inventory and not c.inventory_available:
        v.append("inventory unavailable")
    return tuple(v)


def decide_offer(subject: str, eligible_offers: Sequence[Offer], *, constraints: OfferConstraints,
                 expected_value: Optional[Callable[[Offer], float]] = None) -> OfferDecision:
    """Deterministically select the highest-expected-value offer that satisfies ALL hard constraints. Ties break on
    margin, then customer_value, then offer_id. Returns a NO_ACTION decision (selected=None) when none qualify."""
    ev = expected_value or (lambda o: o.value)
    feasible = tuple(o for o in eligible_offers if not _violations(o, constraints))
    if not feasible:
        reasons = "; ".join(f"{o.offer_id}: {', '.join(_violations(o, constraints))}" for o in eligible_offers)
        return OfferDecision(subject=subject, eligible_offers=tuple(eligible_offers), feasible_offers=(),
                             selected_offer=None, policy=constraints.policy,
                             explanation=f"no offer satisfies policy ({reasons})" if reasons
                             else "no eligible offers")
    ranked = sorted(feasible, key=lambda o: (-ev(o), -o.margin, -o.customer_value, o.offer_id))
    best = ranked[0]
    return OfferDecision(subject=subject, eligible_offers=tuple(eligible_offers), feasible_offers=feasible,
                         selected_offer=best, expected_value=round(ev(best), 4), customer_value=best.customer_value,
                         margin=best.margin, policy=constraints.policy,
                         explanation=f"selected {best.offer_id} (ev={round(ev(best), 4)}, margin={best.margin}) "
                                     f"over {len(feasible) - 1} other feasible offer(s)")


__all__ = ["Offer", "OfferConstraints", "OfferDecision", "decide_offer"]
