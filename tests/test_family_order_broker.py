"""Order Intelligence through the broker + promise-feasibility (Intelligence-APIs Phase 1). Offline.

A DecisionNeed for ORDER_LINEAGE / ORDER_BLOCKERS / PROMISE_FEASIBILITY resolves through the same
resolve_decision_need path to the cost-0 internal order provider, and the order synthesizer states the
answer. promise_feasibility reuses the supplier on-time prior when a supply source is supplied.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.order import SalesOrder, SalesOrderLine, Shipment
from agentic_os.integrations.business.supply import GoodsReceipt, PurchaseOrder, SupplierCommitment
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    InMemoryOrderGraph, InMemorySupplyEvents, OrderIntelligenceProvider, order_registry, order_synthesize,
    promise_feasibility,
)
from runtime_contracts.protocol import Capability, DecisionNeed, ProviderFamily

SUP = "sup:acme"


def _p(ref):
    return Provenance(provider="erp", provider_ref=ref)


def _graph(*, confirmed="2026-01-20", received=False, shipped=None, unconfirmed=False):
    commits = [] if unconfirmed else [SupplierCommitment(prov=_p("c-po1"), po_ref="po1", supplier_ref=SUP,
                                                         confirmed_date=confirmed,
                                                         committed_at="2026-01-03T00:00:00Z")]
    receipts = [GoodsReceipt(prov=_p("r-po1"), po_ref="po1", supplier_ref=SUP, received_date="2026-01-19",
                             quantity=10.0)] if received else []
    ships = [Shipment(prov=_p("ship1"), order_ref="so1", shipped_date=shipped, status="in_transit")] if shipped else []
    return InMemoryOrderGraph(
        _sales_orders=[SalesOrder(prov=_p("so1"), customer_ref="c", promised_date="2026-02-01",
                                  ordered_at="2026-01-01T00:00:00Z", status="open")],
        _lines=[SalesOrderLine(prov=_p("L1"), order_ref="so1", line_no="L1", part="P", quantity=10.0,
                               po_refs=("po1",))],
        _pos=[PurchaseOrder(prov=_p("po1"), supplier_ref=SUP, site="A", part="P", quantity=10.0,
                            promised_date="2026-01-20", ordered_at="2026-01-02T00:00:00Z", status="open")],
        _commitments=commits, _receipts=receipts, _shipments=ships)


def _supply_on_time(n=5):
    orders = [PurchaseOrder(prov=Provenance(provider="erp", provider_ref=f"h{i}"), supplier_ref=SUP,
                            site="A", part="P", quantity=10.0, promised_date="2025-12-10",
                            ordered_at="2025-12-01T00:00:00Z") for i in range(n)]
    receipts = [GoodsReceipt(prov=Provenance(provider="erp", provider_ref=f"hr{i}"), po_ref=f"h{i}",
                             supplier_ref=SUP, received_date="2025-12-10", quantity=10.0) for i in range(n)]
    return InMemorySupplyEvents(orders, receipts)


# ── promise_feasibility ─────────────────────────────────────────────────────────────────────────────────
def test_feasible_when_commitment_precedes_promise_uses_supplier_prior():
    pf = promise_feasibility("so1", _graph(confirmed="2026-01-20"), _supply_on_time(5))
    assert pf.p_on_time == 1.0                       # supplier on-time rate 1.0, ready 2026-01-23 ≤ promised
    assert pf.earliest_credible_date == "2026-01-23" # confirmed 2026-01-20 + 3d ship lead
    assert pf.confidence == round(5 / 15, 4)


def test_commitment_after_promise_is_unlikely():
    pf = promise_feasibility("so1", _graph(confirmed="2026-02-10"), _supply_on_time(5))
    assert pf.p_on_time == 0.1 and pf.earliest_credible_date == "2026-02-13"
    assert "after promised" in pf.drivers[0]


def test_unconfirmed_po_leaves_completion_unbounded():
    pf = promise_feasibility("so1", _graph(unconfirmed=True), _supply_on_time(5))
    assert pf.earliest_credible_date == "" and pf.p_on_time == 0.1
    assert "unbounded" in pf.drivers[0]


def test_shipped_on_time_is_certain():
    pf = promise_feasibility("so1", _graph(received=True, shipped="2026-01-28"))
    assert pf.p_on_time == 1.0 and "on time" in pf.drivers[0]


def test_without_supply_history_uses_a_default_prior():
    pf = promise_feasibility("so1", _graph(confirmed="2026-01-20"))   # no supply source
    assert pf.p_on_time == 0.7 and pf.confidence == 0.5


# ── broker path ─────────────────────────────────────────────────────────────────────────────────────────
def _need(cap, **kw):
    base = dict(decision_case_id="dc1", capability=cap, question="will so1 ship on time?",
                objective="order_review", subject_refs=("so1",), tenant="t", min_confidence=0.0,
                as_of="2026-01-25T00:00:00Z", known_at="2026-01-25T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


def test_promise_feasibility_resolves_through_the_broker():
    reg = order_registry(_graph(confirmed="2026-01-20"), _supply_on_time(5))
    res, _ = resolve_decision_need(reg, _need(Capability.PROMISE_FEASIBILITY), synthesize=order_synthesize)
    assert "P(on-time) 100%" in res.answer and "earliest credible 2026-01-23" in res.answer
    assert res.total_cost == 0.0 and res.provider_receipts[0].provider == "internal.order_intelligence"
    assert res.metrics["p_on_time"] == 1.0


def test_blockers_resolve_through_the_broker():
    reg = order_registry(_graph(confirmed="2026-01-20"))   # confirmed, not received → awaiting receipt
    res, _ = resolve_decision_need(reg, _need(Capability.ORDER_BLOCKERS), synthesize=order_synthesize)
    assert "1 blocker(s)" in res.answer and "not yet received" in res.answer
    assert res.metrics["count"] == 1


def test_lineage_resolves_through_the_broker():
    reg = order_registry(_graph(received=True, shipped="2026-01-28"))
    res, _ = resolve_decision_need(reg, _need(Capability.ORDER_LINEAGE), synthesize=order_synthesize)
    assert "complete" in res.answer and "shipment" in res.answer


def test_internal_order_provider_is_cost_zero_and_internal_family():
    p = OrderIntelligenceProvider(_graph())
    assert p.family is ProviderFamily.INTERNAL_COMPUTED
    assert p.estimate_cost(_need(Capability.ORDER_LINEAGE).to_evidence_request()).money == 0.0
