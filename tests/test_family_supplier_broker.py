"""Supplier reliability served through the broker as an internal provider (Intelligence-APIs Phase 1).

A DecisionNeed for a supplier capability resolves — through the same resolve_decision_need path as any
external provider — to the cost-0 internal metrics provider, and the supplier synthesizer turns the metric
into a business answer with a receipt, lineage and value accounting. All offline.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import GoodsReceipt, PurchaseOrder, SupplierCommitment
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    InMemorySupplyEvents, SupplierMetricsProvider, supplier_registry, supplier_synthesize,
)
from runtime_contracts.protocol import Capability, DecisionNeed, ProviderFamily

SUP = "sup:acme"


def _po(ref, promised, qty=100.0, ordered_at="2026-01-01T00:00:00Z"):
    return PurchaseOrder(prov=Provenance(provider="erp", provider_ref=ref), supplier_ref=SUP, site="A",
                         part="P-1", quantity=qty, promised_date=promised, ordered_at=ordered_at)


def _rcpt(po_ref, received, qty=100.0):
    return GoodsReceipt(prov=Provenance(provider="erp", provider_ref=f"r-{po_ref}"), po_ref=po_ref,
                        supplier_ref=SUP, received_date=received, quantity=qty)


def _commit(po_ref, committed_at, changes=0):
    return SupplierCommitment(prov=Provenance(provider="erp", provider_ref=f"c-{po_ref}"), po_ref=po_ref,
                              supplier_ref=SUP, committed_at=committed_at, promise_change_count=changes)


def _events(n_on_time=5):
    orders = [_po(f"po{i}", "2026-01-10") for i in range(n_on_time)]
    receipts = [_rcpt(f"po{i}", "2026-01-10") for i in range(n_on_time)]   # all on-time, in-full
    return InMemorySupplyEvents(orders, receipts)


def _need(cap=Capability.DELIVERY_RELIABILITY, **kw):
    base = dict(decision_case_id="dc1", capability=cap, question="how reliable is acme?",
                objective="supplier_review", subject_refs=(SUP,), tenant="t", min_confidence=0.2,
                as_of="2026-01-11T00:00:00Z", known_at="2026-01-11T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


def test_delivery_reliability_resolves_through_the_broker():
    res, trace = resolve_decision_need(supplier_registry(_events(5)), _need(), synthesize=supplier_synthesize)
    assert "OTIF 100%" in res.answer and "over 5 orders" in res.answer
    assert res.total_cost == 0.0                                    # internal evidence is free
    assert len(res.provider_receipts) == 1
    r = res.provider_receipts[0]
    assert r.provider == "internal.supplier_metrics" and r.ok and r.cost == 0.0
    assert res.metrics["otif"] == 1.0 and res.metrics["n"] == 5
    assert len(res.evidence_event_ids) == 1
    assert res.tenant == "t" and res.as_of == "2026-01-11T00:00:00Z"
    assert res.is_decision_grade(_need())                          # meets min_confidence 0.2, no gaps


def test_internal_provider_is_cost_zero_and_internal_family():
    p = SupplierMetricsProvider(_events(3))
    assert p.family is ProviderFamily.INTERNAL_COMPUTED
    assert p.estimate_cost(_need().to_evidence_request()).money == 0.0
    assert p.check_entitlement("any", Capability.DELIVERY_RELIABILITY)


def test_confirmation_reliability_resolves_and_synthesizes():
    ev = InMemorySupplyEvents(
        [_po("po1", "2026-01-10"), _po("po2", "2026-01-10")],
        [], [_commit("po1", "2026-01-01T12:00:00Z"), _commit("po2", "2026-01-02T00:00:00Z", changes=1)])
    res, _ = resolve_decision_need(supplier_registry(ev), _need(cap=Capability.CONFIRMATION_RELIABILITY),
                                   synthesize=supplier_synthesize)
    assert "confirms in 18" in res.answer and "moved on 50%" in res.answer   # (12+24)/2 = 18h; 1/2 changed


def test_thin_sample_is_flagged_as_a_gap():
    res, _ = resolve_decision_need(supplier_registry(_events(3)), _need(min_confidence=0.0),
                                   synthesize=supplier_synthesize)
    assert "thin sample (<5 orders)" in res.unresolved_gaps


def test_no_events_yields_no_match_and_a_gap():
    res, trace = resolve_decision_need(supplier_registry(InMemorySupplyEvents()), _need(),
                                       synthesize=supplier_synthesize)
    assert res.answer == "" and res.unresolved_gaps == ("no evidence acquired",)
    assert res.total_cost == 0.0 and res.provider_receipts and not res.provider_receipts[0].ok


def test_as_of_scoped_source_is_leakage_safe():
    # a receipt knowable only later must not reach a decision taken before it.
    orders = [PurchaseOrder(prov=Provenance(provider="erp", provider_ref="po1", known_at=1000),
                            supplier_ref=SUP, site="A", part="P", quantity=100.0, promised_date="2026-01-10",
                            ordered_at="2026-01-01T00:00:00Z")]
    receipts = [GoodsReceipt(prov=Provenance(provider="erp", provider_ref="r1", known_at=5000), po_ref="po1",
                             supplier_ref=SUP, received_date="2026-01-10", quantity=100.0)]
    early = InMemorySupplyEvents(orders, receipts, as_of_ms=3000)   # before the receipt is knowable
    res, _ = resolve_decision_need(supplier_registry(early), _need(min_confidence=0.0),
                                   synthesize=supplier_synthesize)
    assert res.unresolved_gaps == ("no evidence acquired",)        # no receipt yet ⇒ no metric
