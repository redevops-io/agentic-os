"""DealState producer + capability bindings make the substrate CALLABLE (P5).

Proves: produce_verified_deal attaches reported-vs-verified claims (equal→VERIFIED, differ→CONFLICTED) so the
existing honest status works; and bind_commercial_capabilities wires executable handlers onto the registry so
offer/campaign/funnel/deal_state capabilities actually run via registry.get(id).handler(...).
"""
from __future__ import annotations

from agentic_os.commercial import (
    CampaignHypothesis, FieldReadback, Offer, OfferConstraints, OfferDecision, bind_commercial_capabilities,
    produce_verified_deal,
)
from agentic_os.deal_closing.contracts import ClaimStatus, Deal, Provenance
from agentic_os.sidekick import CapabilityRegistry, register_builtin_capabilities


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
