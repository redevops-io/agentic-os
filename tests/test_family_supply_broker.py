"""Supply Intelligence through the broker (Intelligence-APIs Phase 2). Offline.

A DecisionNeed for any Supply capability resolves — through the same resolve_decision_need path as any
external provider — to the cost-0 internal supply provider, and the supply synthesizer states the answer.
Parameterised capabilities read qty / need_by / lead_time from "key=value" subject tokens.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.order import SalesOrder, SalesOrderLine
from agentic_os.integrations.business.supply import (
    BOMLine, DemandRequirement, InventoryPosition, PurchaseOrder,
)
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    SupplyGraph, SupplyIntelligenceProvider, supply_registry, supply_synthesize,
)
from runtime_contracts.protocol import Capability, DecisionNeed, ProviderFamily


def _pr(ref):
    return Provenance(provider="erp", provider_ref=ref)


def _graph():
    return SupplyGraph(
        inventory=[InventoryPosition(prov=_pr("inv-Rim"), part="Rim", site="A", on_hand=20.0)],
        open_supply=[PurchaseOrder(prov=_pr("po1"), supplier_ref="s", site="A", part="Rim", quantity=50.0,
                                   promised_date="2026-02-15", ordered_at="2026-01-01T00:00:00Z")],
        demand=[DemandRequirement(prov=_pr("d1"), part="Rim", site="A", need_date="2026-02-10", quantity=40.0),
                DemandRequirement(prov=_pr("d2"), part="Rim", site="A", need_date="2026-02-20", quantity=60.0)],
        bom_lines=[BOMLine(prov=_pr("Bike->Wheel"), parent_part="Bike", component_part="Wheel", quantity_per=2.0),
                   BOMLine(prov=_pr("Wheel->Rim"), parent_part="Wheel", component_part="Rim", quantity_per=1.0),
                   BOMLine(prov=_pr("Wheel->RimAlt"), parent_part="Wheel", component_part="RimAlt",
                           quantity_per=1.0, is_substitute=True, substitute_for="Rim")],
        order_lines=[SalesOrderLine(prov=_pr("so1-Bike"), order_ref="so1", part="Bike", quantity=1.0)],
        sales_orders=[SalesOrder(prov=_pr("so1"), customer_ref="c", promised_date="2026-03-01", status="open")],
    )


def _need(cap, subject_refs):
    return DecisionNeed(decision_case_id="dc1", capability=cap, question="?", objective="supply_review",
                        subject_refs=subject_refs, tenant="t", min_confidence=0.0,
                        as_of="2026-01-25T00:00:00Z", known_at="2026-01-25T00:00:00Z")


def _resolve(cap, subject_refs):
    return resolve_decision_need(supply_registry(_graph()), _need(cap, subject_refs),
                                 synthesize=supply_synthesize)


def test_shortage_risk_resolves():
    res, _ = _resolve(Capability.SHORTAGE_RISK, ("Rim", "A"))
    assert "first shortage 2026-02-10" in res.answer and res.total_cost == 0.0
    assert res.provider_receipts[0].provider == "internal.supply_intelligence"
    assert res.metrics["first_shortage_date"] == "2026-02-10"


def test_required_by_feasibility_resolves_with_params():
    res, _ = _resolve(Capability.REQUIRED_BY_FEASIBILITY,
                      ("part=Rim", "site=A", "qty=60", "need_by=2026-03-01"))
    assert "feasible" in res.answer and res.metrics["feasible"] is True
    assert res.metrics["available_by_need"] == 70.0            # net 20 + po1 50


def test_bom_impact_resolves():
    res, _ = _resolve(Capability.BOM_IMPACT, ("Rim",))
    assert list(res.metrics["affected_assemblies"]) == ["Bike", "Wheel"]
    assert "affects 2 assembl" in res.answer


def test_substitute_availability_resolves():
    res, _ = _resolve(Capability.SUBSTITUTE_AVAILABILITY, ("Rim", "A"))
    assert res.metrics["count"] == 1 and res.metrics["substitutes"][0]["part"] == "RimAlt"


def test_stockout_consequence_resolves():
    res, _ = _resolve(Capability.STOCKOUT_CONSEQUENCE, ("Rim",))
    assert res.metrics["orders_delayed"] == 1 and "so1" in res.metrics["affected_orders"]
    assert res.metrics["earliest_impact_date"] == "2026-03-01"


def test_safety_stock_resolves_as_a_forecast():
    res, _ = _resolve(Capability.SAFETY_STOCK, ("part=Rim", "site=A", "lead_time_days=4", "service_level=0.95"))
    assert "recommend safety stock" in res.answer and "Forecast recommendation" in res.answer
    assert res.confidence == 0.5                              # a forecast, modest confidence — not a hard fact


def test_provider_is_cost_zero_and_internal_family():
    p = SupplyIntelligenceProvider(_graph())
    assert p.family is ProviderFamily.INTERNAL_COMPUTED
    assert p.estimate_cost(_need(Capability.SHORTAGE_RISK, ("Rim",)).to_evidence_request()).money == 0.0
