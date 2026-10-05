"""Governed commerce execution (Phase 8) — recommend → approve → execute → verify, never below the floor.

The write half, done safely. A price change is applied only when (1) it passes a PricingPolicy of HARD
constraints a model can never override (contribution-margin floor, MAP, max change size), (2) it is explicitly
approved, and (3) it is observed in the listing by READ-BACK — a 200 from the marketplace is not success, the
listing showing the new price is (the same obligation discipline as the Integration plane). Anything else is
refused or flagged, nothing is assumed.

Provider-neutral: workflows request ``CommerceSurfaceProvider`` capabilities, not a vendor's endpoint; the real
Amazon/Shopify/… write adapters are the enterprise overlay. Policy-authorized (no human) execution is a later
step layered on the same guard — this module ships recommend/approval-gated execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class CommerceSurfaceProvider(Protocol):
    """Write surface for a marketplace/commerce listing. ``verify_listing`` is the read-back used to prove a
    write landed. Capabilities vary by marketplace — a workflow asks for these, not a vendor method."""
    provider: str

    def read_listing(self, listing_id: str) -> Optional[dict]: ...
    def update_price(self, listing_id: str, unit_price: float, *, idempotency_key: str = "") -> bool: ...
    def verify_listing(self, listing_id: str) -> Optional[dict]: ...


@dataclass(frozen=True)
class PricingPolicy:
    """Hard constraints enforced before any write. A model cannot override these (plan §12/§24)."""
    unit_cost: float = 0.0
    min_margin: float = 0.30             # contribution-margin floor
    min_price: float = 0.0               # absolute floor (e.g. a vendor minimum)
    map_price: float = 0.0               # minimum advertised price, where applicable
    max_change_pct: float = 0.0          # max |Δ| vs current as a fraction (0 = no cap)

    def floor_price(self) -> Optional[float]:
        if self.unit_cost <= 0 or not (0.0 <= self.min_margin < 1.0):
            return None
        return round(self.unit_cost / (1.0 - self.min_margin), 4)

    def check(self, current_price: Optional[float], proposed: float) -> "Tuple[bool, str]":
        floor = self.floor_price()
        if floor is not None and proposed < floor:
            return False, f"below contribution-margin floor {floor}"
        if self.min_price and proposed < self.min_price:
            return False, f"below minimum price {self.min_price}"
        if self.map_price and proposed < self.map_price:
            return False, f"below MAP {self.map_price}"
        if self.max_change_pct and current_price not in (None, 0) and \
                abs(proposed - current_price) / current_price > self.max_change_pct:
            return False, f"change exceeds max {self.max_change_pct:.0%} vs current {current_price}"
        return True, "ok"


@dataclass(frozen=True)
class PriceChangeResult:
    status: str                          # BLOCKED | PENDING_APPROVAL | APPLIED | UNVERIFIED
    reason: str
    listing_id: str
    proposed_unit_price: float
    current_unit_price: Optional[float] = None
    verified_unit_price: Optional[float] = None
    blocked_by: str = ""

    @property
    def applied(self) -> bool:
        return self.status == "APPLIED"


def apply_price_change(provider: CommerceSurfaceProvider, policy: PricingPolicy, listing_id: str,
                       proposed_unit_price: float, *, approved: bool = False, authority: str = "svc",
                       tolerance: float = 1e-6) -> PriceChangeResult:
    """Governed price change. Order: hard-constraint check → approval gate → write → read-back verify. The write
    is never trusted on its return value; only the listing showing the new price counts as APPLIED."""
    listing = provider.read_listing(listing_id)
    current = float(listing["unit_price"]) if listing and "unit_price" in listing else None

    ok, reason = policy.check(current, proposed_unit_price)
    if not ok:
        return PriceChangeResult("BLOCKED", reason, listing_id, proposed_unit_price, current, blocked_by=reason)

    if not approved:
        return PriceChangeResult("PENDING_APPROVAL", "passes policy; awaiting approval before any write",
                                 listing_id, proposed_unit_price, current)

    provider.update_price(listing_id, proposed_unit_price,
                          idempotency_key=f"price_{listing_id}_{proposed_unit_price}")
    back = provider.verify_listing(listing_id)
    verified = float(back["unit_price"]) if back and "unit_price" in back else None
    if verified is not None and abs(verified - proposed_unit_price) <= tolerance:
        return PriceChangeResult("APPLIED", "verified by read-back", listing_id, proposed_unit_price,
                                 current, verified_unit_price=verified)
    return PriceChangeResult("UNVERIFIED", "write returned but the listing does not show the new price "
                             "(silent failure caught)", listing_id, proposed_unit_price, current,
                             verified_unit_price=verified)
