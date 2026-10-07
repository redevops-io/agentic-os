"""Funnel & Conversion Intelligence — generalized own-site funnel + market bridge (plan §5-§11, P3).

Proves: a ConversionFunnel is tenant-scoped + provider-neutral; diagnose_funnel finds the leakiest stage
deterministically without asserting causality; the gap→action map is correct; and a market.Opportunity
(the demo.redevops.io/market queue item) bridges into a governed FunnelIntervention — preserving that path.
"""
from __future__ import annotations

from agentic_os.commercial import (
    ConversionFunnel, ConversionStage, FunnelAction, SurfaceType, action_for_gap, diagnose_funnel,
    intervention_from_opportunity,
)


def _funnel():
    return ConversionFunnel(
        funnel_id="redevops.io", tenant_id="t_redevops", objective="demo_requests",
        stages=(ConversionStage("visit", surface_type=SurfaceType.LANDING),
                ConversionStage("pricing", surface_type=SurfaceType.PRICING),
                ConversionStage("demo", surface_type=SurfaceType.DEMO,
                                editable_capabilities=(FunnelAction.CHANGE_CTA,))),
        value_model={"value_per_conversion": 1000.0})


def test_funnel_is_tenant_scoped_and_addressable():
    f = _funnel()
    assert f.tenant_id == "t_redevops"
    assert f.stage("pricing").surface_type is SurfaceType.PRICING
    assert f.stage("missing") is None


def test_diagnose_finds_leakiest_stage_without_asserting_cause():
    f = _funnel()
    diag = diagnose_funnel(f, stage_conversion={"visit": 0.9, "pricing": 0.3, "demo": 0.8},
                           observed_change="pricing→demo dropped")
    assert diag.affected_stage == "pricing"          # lowest conversion
    assert diag.confidence == round(1 - 0.3, 4)
    assert diag.economic_impact == round((1 - 0.3) * 1000.0, 4)
    assert "offer_unclear" in diag.candidate_causes  # candidate, not asserted
    # ties resolve to the earliest stage deterministically
    tie = diagnose_funnel(f, stage_conversion={"visit": 0.5, "pricing": 0.5, "demo": 0.9})
    assert tie.affected_stage == "visit"


def test_action_for_gap_mapping():
    assert action_for_gap("offer_pattern:discount") is FunnelAction.CHANGE_OFFER
    assert action_for_gap("cta_pattern:signup") is FunnelAction.CHANGE_CTA
    assert action_for_gap("pricing:anchor") is FunnelAction.CHANGE_PRICE_PRESENTATION
    assert action_for_gap("social_proof:logos") is FunnelAction.ADD_SOCIAL_PROOF
    assert action_for_gap("totally_new_thing") is FunnelAction.CREATE_VARIANT


class _Opp:
    """Duck-typed stand-in for market.Opportunity (the demo.redevops.io/market queue item)."""
    site = "redevops.io"
    gap = "offer_pattern:discount"
    proposed_experiment = "Test a discount offer on the acquisition page"
    expected_value = 0.833
    reversibility = "reversible"
    evidence_refs = ("langchain:offer:discount", "llamaindex:offer:discount")
    confidence = 0.8


def test_market_opportunity_bridges_to_funnel_intervention():
    iv = intervention_from_opportunity(_Opp(), stage_id="pricing")
    assert iv.action is FunnelAction.CHANGE_OFFER
    assert iv.funnel_id == "redevops.io" and iv.stage_id == "pricing"
    assert iv.expected_value == 0.833 and iv.reversibility == "reversible"
    assert iv.evidence == ("langchain:offer:discount", "llamaindex:offer:discount")
    assert iv.source == "market.opportunity"
