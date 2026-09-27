"""Unified intelligence service — the §3 API core (resolve / quote / get) across families. Offline."""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.order import SalesOrder, SalesOrderLine, Shipment
from agentic_os.integrations.business.supply import GoodsReceipt, InventoryPosition, PurchaseOrder
from agentic_os.intelligence import IntelligenceService, resolve_decision_need
from agentic_os.intelligence.adapters.gleif import GleifProvider
from agentic_os.intelligence.families import (
    InMemoryOrderGraph, SupplyGraph, order_registry, order_synthesize, supply_registry, supply_synthesize,
)
from runtime_contracts.protocol import Capability, DecisionNeed, HealthStatus, provider_health


def _pr(ref):
    return Provenance(provider="erp", provider_ref=ref)


def _service():
    supply = SupplyGraph(inventory=[InventoryPosition(prov=_pr("i"), part="Rim", site="A", on_hand=20.0)],
                         open_supply=[PurchaseOrder(prov=_pr("po1"), supplier_ref="s", site="A", part="Rim",
                                                    quantity=50.0, promised_date="2026-02-15",
                                                    ordered_at="2026-01-01T00:00:00Z")])
    order = InMemoryOrderGraph(
        _sales_orders=[SalesOrder(prov=_pr("so1"), customer_ref="c", promised_date="2026-03-01", status="open")],
        _lines=[SalesOrderLine(prov=_pr("L1"), order_ref="so1", part="Rim", quantity=10.0, po_refs=("po1",))],
        _pos=[PurchaseOrder(prov=_pr("po1"), supplier_ref="s", site="A", part="Rim", quantity=10.0,
                            promised_date="2026-01-20", ordered_at="2026-01-02T00:00:00Z")],
        _receipts=[GoodsReceipt(prov=_pr("r1"), po_ref="po1", supplier_ref="s", received_date="2026-01-19",
                                quantity=10.0)],
        _shipments=[Shipment(prov=_pr("sh1"), order_ref="so1", shipped_date="2026-01-28", status="in_transit")])
    svc = IntelligenceService()
    svc.bind(supply_registry(supply), supply_synthesize)
    svc.bind(order_registry(order), order_synthesize)
    return svc


def _need(cap, subject_refs):
    return DecisionNeed(decision_case_id="dc1", capability=cap, question="?", objective="review",
                        subject_refs=subject_refs, tenant="t", min_confidence=0.0)


def test_service_routes_a_capability_to_the_right_family():
    svc = _service()
    # a supply capability and an order capability both resolve through the same service entry point.
    sr = svc.resolve(_need(Capability.SHORTAGE_RISK, ("Rim", "A")))
    assert sr.metrics["part"] == "Rim" and sr.provider_receipts[0].provider == "internal.supply_intelligence"
    orr = svc.resolve(_need(Capability.ORDER_LINEAGE, ("so1",)))
    assert "complete" in orr.answer and orr.provider_receipts[0].provider == "internal.order_intelligence"


def test_get_returns_a_stored_result_by_fingerprint():
    svc = _service()
    res = svc.resolve(_need(Capability.SHORTAGE_RISK, ("Rim", "A")))
    assert svc.get(res.fingerprint()) is res
    assert svc.get("nope") is None


def test_quote_estimates_before_execution():
    svc = _service()
    q = svc.quote(_need(Capability.SHORTAGE_RISK, ("Rim", "A")))
    assert q.available and q.expected_cost == 0.0 and "internal.supply_intelligence" in q.providers


def test_quote_for_an_unserved_capability_is_unavailable():
    q = _service().quote(_need(Capability.LEGAL_RESEARCH, ("x",)))
    assert not q.available and q.reason == "capability not served"


def test_unserved_capability_resolves_to_a_gap_not_a_crash():
    res = _service().resolve(_need(Capability.LEGAL_RESEARCH, ("x",)))
    assert res.unresolved_gaps == ("capability not served",) and res.total_cost == 0.0


def test_gleif_reports_health_ok_via_the_seam():
    # the adapter now implements the optional SupportsHealth seam (was UNKNOWN before).
    assert provider_health(GleifProvider()).status is HealthStatus.OK
