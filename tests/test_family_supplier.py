"""Supplier Intelligence deterministic metrics (Intelligence-APIs plan §5, Phase-1 exit gate).

Pins the exit-gate guarantee: supplier reliability reproduces EXACTLY from raw PO / confirmation / receipt
events, is leakage-safe as-of a past decision time, scopes to a site, and reports confidence that rises
with sample size. All offline, pure functions.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import GoodsReceipt, PurchaseOrder, SupplierCommitment
from agentic_os.intelligence.families import confirmation_reliability, delivery_reliability

SUP = "sup:acme"


def _po(ref, promised, qty=100.0, site="A", ordered_at="2026-01-01T00:00:00Z", known_at=0):
    return PurchaseOrder(prov=Provenance(provider="erp", provider_ref=ref, known_at=known_at),
                         supplier_ref=SUP, site=site, part="P-1", quantity=qty, promised_date=promised,
                         ordered_at=ordered_at, status="received")


def _rcpt(po_ref, received, qty=100.0, known_at=0):
    return GoodsReceipt(prov=Provenance(provider="erp", provider_ref=f"r-{po_ref}", known_at=known_at),
                        po_ref=po_ref, supplier_ref=SUP, received_date=received, quantity=qty)


def _commit(po_ref, committed_at, changes=0):
    return SupplierCommitment(prov=Provenance(provider="erp", provider_ref=f"c-{po_ref}"),
                              po_ref=po_ref, supplier_ref=SUP, committed_at=committed_at,
                              promise_change_count=changes)


# Deterministic fixture: 5 orders all promised 2026-01-10.
def _orders():
    return [_po("po1", "2026-01-10"), _po("po2", "2026-01-10"), _po("po3", "2026-01-10"),
            _po("po4", "2026-01-10"), _po("po5", "2026-01-10")]


def _receipts():
    return [
        _rcpt("po1", "2026-01-10"),            # on-time, in-full   → OTIF
        _rcpt("po2", "2026-01-12"),            # 2 late, in-full
        _rcpt("po3", "2026-01-08"),            # 2 early, in-full   → OTIF
        _rcpt("po4", "2026-01-10", qty=80.0),  # on-time, SHORT (not in-full)
        _rcpt("po5", "2026-01-15"),            # 5 late, in-full
    ]


def test_delivery_reliability_reproduces_from_raw_events():
    d = delivery_reliability(_orders(), _receipts(), supplier_ref=SUP)
    assert d.n == 5
    assert d.on_time_rate == 0.6          # po1, po3, po4
    assert d.in_full_rate == 0.8          # all but po4
    assert d.otif == 0.4                  # only po1, po3 are on-time AND in-full
    assert d.mean_days_late == 1.0        # (0 + 2 + -2 + 0 + 5) / 5
    assert d.p50_days_late == 0.0
    assert d.p90_days_late == 5.0
    assert d.scope == "supplier" and d.confidence == round(5 / 15, 4)


def test_as_of_excludes_events_not_yet_knowable():
    # Explicit fact times: everything is knowable by t=1000 except po5's receipt (t=5000). A decision
    # taken as-of t=3000 must not see the future receipt.
    orders = [_po(f"po{i}", "2026-01-10", known_at=1000) for i in range(1, 6)]
    receipts = [
        _rcpt("po1", "2026-01-10", known_at=1000), _rcpt("po2", "2026-01-12", known_at=1000),
        _rcpt("po3", "2026-01-08", known_at=1000), _rcpt("po4", "2026-01-10", qty=80.0, known_at=1000),
        _rcpt("po5", "2026-01-15", known_at=5000),          # not yet knowable at t=3000
    ]
    d = delivery_reliability(orders, receipts, supplier_ref=SUP, as_of_ms=3000)
    assert d.n == 4                       # po5 excluded — no leakage of the future receipt
    assert d.on_time_rate == 0.75 and d.otif == 0.5
    assert d.p90_days_late == 2.0         # the 5-late outlier is gone


def test_site_scope_restricts_the_pool():
    orders = _orders() + [_po("poB", "2026-01-10", site="B")]
    receipts = _receipts() + [_rcpt("poB", "2026-02-01")]   # very late, other site
    d = delivery_reliability(orders, receipts, supplier_ref=SUP, site="A")
    assert d.n == 5 and d.scope == "supplier-site"          # site B order not counted


def test_partial_receipt_is_not_in_full():
    d = delivery_reliability([_po("x", "2026-01-10", qty=100.0)], [_rcpt("x", "2026-01-09", qty=50.0)],
                             supplier_ref=SUP)
    assert d.on_time_rate == 1.0 and d.in_full_rate == 0.0 and d.otif == 0.0


def test_confirmation_reliability_reproduces_from_raw_events():
    orders = [_po("po1", "2026-01-10"), _po("po2", "2026-01-10"), _po("po3", "2026-01-10")]
    commits = [_commit("po1", "2026-01-01T12:00:00Z"),          # 12h, no change
               _commit("po2", "2026-01-02T00:00:00Z", changes=1),  # 24h, moved once
               _commit("po3", "2026-01-01T06:00:00Z")]          # 6h, no change
    c = confirmation_reliability(orders, commits, supplier_ref=SUP)
    assert c.n == 3
    assert c.mean_confirm_latency_h == 14.0     # (12 + 24 + 6) / 3
    assert c.promise_change_rate == round(1 / 3, 4)


def test_confidence_rises_with_sample_size():
    small = delivery_reliability([_po("a", "2026-01-10")], [_rcpt("a", "2026-01-10")], supplier_ref=SUP)
    big_orders = [_po(f"o{i}", "2026-01-10") for i in range(30)]
    big_receipts = [_rcpt(f"o{i}", "2026-01-10") for i in range(30)]
    big = delivery_reliability(big_orders, big_receipts, supplier_ref=SUP)
    assert big.confidence > small.confidence
    assert big.otif == 1.0 and small.otif == 1.0


def test_no_events_is_zeroed_not_an_error():
    d = delivery_reliability([], [], supplier_ref=SUP)
    assert d.n == 0 and d.otif == 0.0 and d.confidence == 0.0
