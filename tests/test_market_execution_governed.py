"""Market & Funnel Intelligence Phase 8 — governed commerce execution.

Proves: a price change is BLOCKED below the margin floor / MAP / max-change; PENDING_APPROVAL until approved
(no write happens); APPLIED only when the listing shows the new price by read-back; and a silent write failure
(returns true but the listing is unchanged) is caught as UNVERIFIED, never assumed applied.
"""
from __future__ import annotations

from agentic_os.market import CommerceSurfaceProvider, PricingPolicy, apply_price_change


class _FakeCommerce:
    provider = "fake_marketplace"

    def __init__(self, price, *, drop_writes=False):
        self.store = {"L1": {"unit_price": price}}
        self.drop_writes = drop_writes
        self.writes = 0

    def read_listing(self, listing_id):
        return dict(self.store.get(listing_id)) if listing_id in self.store else None

    def update_price(self, listing_id, unit_price, *, idempotency_key=""):
        self.writes += 1
        if not self.drop_writes:
            self.store[listing_id]["unit_price"] = unit_price
        return True                      # marketplace returns ok regardless — only read-back is trusted

    def verify_listing(self, listing_id):
        return self.read_listing(listing_id)


def _policy():
    return PricingPolicy(unit_cost=10.0, min_margin=0.3, map_price=12.0, max_change_pct=0.25)  # floor 14.29


def test_is_a_provider():
    assert isinstance(_FakeCommerce(20.0), CommerceSurfaceProvider)


def test_blocked_below_margin_floor():
    p = _FakeCommerce(20.0)
    r = apply_price_change(p, _policy(), "L1", 13.0, approved=True)   # 13 < floor 14.29
    assert r.status == "BLOCKED" and "floor" in r.reason and p.writes == 0


def test_blocked_by_max_change():
    p = _FakeCommerce(20.0)
    r = apply_price_change(p, _policy(), "L1", 14.5, approved=True)   # |20→14.5|/20 = 27.5% > 25%
    assert r.status == "BLOCKED" and "max" in r.reason and p.writes == 0


def test_pending_approval_does_not_write():
    p = _FakeCommerce(20.0)
    r = apply_price_change(p, _policy(), "L1", 17.0, approved=False)  # passes policy, not approved
    assert r.status == "PENDING_APPROVAL" and p.writes == 0 and p.store["L1"]["unit_price"] == 20.0


def test_applied_when_verified_by_readback():
    p = _FakeCommerce(20.0)
    r = apply_price_change(p, _policy(), "L1", 17.0, approved=True)
    assert r.applied and r.verified_unit_price == 17.0 and p.store["L1"]["unit_price"] == 17.0


def test_silent_write_failure_is_unverified():
    p = _FakeCommerce(20.0, drop_writes=True)                        # returns true but never persists
    r = apply_price_change(p, _policy(), "L1", 17.0, approved=True)
    assert r.status == "UNVERIFIED" and p.store["L1"]["unit_price"] == 20.0
