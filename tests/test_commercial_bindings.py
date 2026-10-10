"""DealState producer + capability bindings make the substrate CALLABLE (P5).

Proves: produce_verified_deal attaches reported-vs-verified claims (equal→VERIFIED, differ→CONFLICTED) so the
existing honest status works; and bind_commercial_capabilities wires executable handlers onto the registry so
offer/campaign/funnel/deal_state capabilities actually run via registry.get(id).handler(...).
"""
from __future__ import annotations

from agentic_os.commercial import (
    CampaignHypothesis, DeliveryState, FieldReadback, Offer, OfferConstraints, OfferDecision, OrderExecution,
    OrderLine, PaymentState, ReceivableAction, ReceivableCase, ServiceRequirement, ServiceResource,
    bind_commercial_capabilities, produce_verified_deal,
)
from agentic_os.deal_closing.contracts import ClaimStatus, Deal, Provenance
from agentic_os.sidekick import CapabilityRegistry, SidekickOrchestrator, register_builtin_capabilities


def test_produce_verified_deal_attaches_claims_with_honest_status():
    deal = Deal(prov=Provenance(provider="salesforce"), reported_stage="negotiation",
                reported_amount_cents=50000, reported_probability=0.8, source_systems=("salesforce",))
    verified = produce_verified_deal(deal, [
        FieldReadback("stage", "negotiation", source="runtime.readback", verified_at=1000),   # agrees
        FieldReadback("amount_cents", 40000, source="erpnext", verified_at=1000),              # disagrees
    ])
    by_field = {c.field_name: c for c in verified.claims}
    assert by_field["stage"].status() is ClaimStatus.VERIFIED
    assert by_field["amount_cents"].status() is ClaimStatus.CONFLICTED
    assert by_field["stage"].reported == "negotiation" and by_field["stage"].verified == "negotiation"
    # conflicts surface via the existing Deal helper
    assert any(c.field_name == "amount_cents" for c in verified.conflicts())


def test_produce_verified_deal_replaces_existing_field_claim():
    deal = Deal(prov=Provenance(provider="sf"), reported_stage="proposal")
    once = produce_verified_deal(deal, [FieldReadback("stage", "proposal", source="s1")])
    twice = produce_verified_deal(once, [FieldReadback("stage", "closed", source="s2")])
    stage_claims = [c for c in twice.claims if c.field_name == "stage"]
    assert len(stage_claims) == 1 and stage_claims[0].verified == "closed"   # replaced, not duplicated


def test_bind_commercial_capabilities_makes_them_callable():
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    bound = bind_commercial_capabilities(reg)
    assert "acquisition.offer_decide" in bound and "sales.deal_state" in bound

    # offer_decide now runs
    cap = reg.get("acquisition.offer_decide")
    assert cap.bound is True
    out = cap.handler(subject="acme",
                      eligible_offers=[Offer("a", value=10, margin=0.3), Offer("b", value=20, margin=0.3)],
                      constraints=OfferConstraints())
    assert isinstance(out, OfferDecision) and out.selected_offer.offer_id == "b"

    # campaign_plan runs
    plan = reg.get("acquisition.campaign_plan").handler(
        hypothesis=CampaignHypothesis(objective="demos", audience="fintech"))
    assert plan.objective == "demos"

    # deal_state producer runs through the registry
    deal = Deal(prov=Provenance(provider="sf"), reported_stage="negotiation", source_systems=("sf",))
    vd = reg.get("sales.deal_state").handler(reported=deal,
                                             readbacks=[FieldReadback("stage", "negotiation", source="rb")])
    assert vd.claims and vd.claims[0].field_name == "stage"


def test_binding_is_idempotent():
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    assert bind_commercial_capabilities(reg) == bind_commercial_capabilities(reg)


def test_operations_and_finance_capabilities_are_bound():
    """The ops/finance plane the wiring-maturity audit flagged TESTED_UNWIRED is now bound + callable."""
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    bound = bind_commercial_capabilities(reg)
    for cid in ("operations.order_execute", "operations.service_plan", "finance.receivables"):
        assert cid in bound, f"{cid} should be bound"
        assert reg.get(cid).bound is True


def test_order_execute_runs_and_projects_through_orchestrator():
    """A late order produces a remediation proposal via the deployed-surface path (orchestrator.invoke → projector)."""
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    bind_commercial_capabilities(reg)
    orc = SidekickOrchestrator(registry=reg)

    order = OrderExecution(order_id="SO-42", customer="Acme", required_by=1000,
                           lines=(OrderLine(sku="widget", quantity=3, unit_price_cents=5000),))
    # 'reserved' transition is overdue (deadline 1000, now 2000) and not yet observed in the target state
    res = orc.invoke("operations.order_execute",
                     order=order, observed_stage_states={"order:SO-42:sourced": "sourced"}, now=2000)
    assert "at risk" in res.answer
    assert res.candidate_actions, "an overdue transition must propose a remediation action"
    assert any(a["action"].startswith("remediate") for a in res.candidate_actions)

    # a healthy order yields no incidents and no actions
    ok = orc.invoke("operations.order_execute", order=order,
                    observed_stage_states={f"order:SO-42:{s}": s for s in
                                           ("sourced", "reserved", "fulfilled", "invoiced", "verified")}, now=500)
    assert "on track" in ok.answer and not ok.candidate_actions


def test_service_plan_runs_and_projects_feasibility():
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    bind_commercial_capabilities(reg)
    orc = SidekickOrchestrator(registry=reg)

    feasible = orc.invoke("operations.service_plan", customer_commitment="C-1", requested_service="install",
                          requirements=(ServiceRequirement(skill="roofing", quantity=1),),
                          resources=(ServiceResource(resource_id="crew-a", skills=("roofing",), capacity=2),))
    assert "feasible" in feasible.answer and feasible.confidence == 1.0

    short = orc.invoke("operations.service_plan", customer_commitment="C-2", requested_service="install",
                       requirements=(ServiceRequirement(skill="electrical", quantity=1),),
                       resources=(ServiceResource(resource_id="crew-a", skills=("roofing",), capacity=2),))
    assert "NOT feasible" in short.answer and short.candidate_actions[0]["action"] == "resolve_resourcing"


def test_receivables_runs_and_projects_action():
    reg = CapabilityRegistry()
    register_builtin_capabilities(reg)
    bind_commercial_capabilities(reg)
    orc = SidekickOrchestrator(registry=reg)

    # delivery incomplete → should resolve delivery, NOT blindly collect
    res = orc.invoke("finance.receivables",
                     case=ReceivableCase(customer="Acme", invoice_id="INV-9", amount_cents=120000, days_overdue=30,
                                         payment_state=PaymentState.OVERDUE, delivery_state=DeliveryState.PARTIAL))
    assert ReceivableAction.RESOLVE_DELIVERY.value in res.answer
    assert res.candidate_actions[0]["action"] == ReceivableAction.RESOLVE_DELIVERY.value
