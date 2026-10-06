"""Product Opportunity Intelligence Phase 8 — outcome learning / discovery priors.

Proves: a result is derived from real usage (behaviour > social proxy); priors are shrunk toward neutral so
sparse evidence doesn't dominate; INCONCLUSIVE carries no signal; and priors reweight future opportunities
(a source/class that validated lifts rank; one that didn't lowers it) without gating.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.product_opportunity import (
    ValidationOutcome, apply_priors, build_opportunity, calibrate, result_from_usage,
)
from agentic_os.product_opportunity.capability_map import CapabilityCoverage
from agentic_os.product_opportunity.cluster import WorkflowCluster
from agentic_os.product_opportunity.contracts import WorkflowPain


def test_result_from_usage_behaviour_over_proxy():
    assert result_from_usage({"launched": 5, "repeated": 3}) == "VALIDATED"
    assert result_from_usage({"paid": 1}) == "VALIDATED"
    assert result_from_usage({}, verified_business_outcome=True) == "VALIDATED"
    assert result_from_usage({"launched": 4, "repeated": 0}) == "INVALIDATED"
    assert result_from_usage({"viewed": 10}) == "INCONCLUSIVE"


def test_priors_are_shrunk_and_inconclusive_ignored():
    outcomes = [
        ValidationOutcome("o1", ("reddit",), "PACKAGE_AS_TEMPLATE", ("stripe", "quickbooks"), "VALIDATED"),
        ValidationOutcome("o2", ("reddit",), "PACKAGE_AS_TEMPLATE", ("stripe", "xero"), "VALIDATED"),
        ValidationOutcome("o3", ("quora",), "NEW_CONNECTOR_REQUIRED", ("magento",), "INVALIDATED"),
        ValidationOutcome("o4", ("x",), "PACKAGE_AS_TEMPLATE", ("shopify",), "INCONCLUSIVE"),  # no signal
    ]
    priors = calibrate(outcomes, strength=4)
    # reddit: 2/2 but shrunk toward 0.5 → below 1.0
    assert 0.5 < priors.source_success("reddit") < 1.0
    # quora: 0/1 → below 0.5
    assert priors.source_success("quora") < 0.5
    # INCONCLUSIVE 'x' contributed no trial
    assert "x" not in priors.by_source
    # unknown source → neutral 0.5
    assert priors.source_success("hackernews") == 0.5


def _opp(classification, apps):
    wp = WorkflowPain(prov=Provenance(provider="t"), name="|".join(apps), applications=tuple(apps),
                      pain_dimensions=("cross_app",), agentic_fit="medium")
    cl = WorkflowCluster(workflow_pain=wp, signature="|".join(apps), raw_mentions=3, independent_evidence_count=3,
                         independent_authors=3, sources=("reddit",), communities=("r/x",), applications=tuple(apps),
                         first_seen=0, last_seen=1)
    return build_opportunity(cl, CapabilityCoverage("wp", tuple(apps), (), (), 1.0, classification))


def test_apply_priors_reweights_without_gating():
    outcomes = [
        ValidationOutcome("a", ("reddit",), "PACKAGE_AS_TEMPLATE", ("stripe", "quickbooks"), "VALIDATED"),
        ValidationOutcome("b", ("reddit",), "PACKAGE_AS_TEMPLATE", ("stripe", "netsuite"), "VALIDATED"),
        ValidationOutcome("c", ("quora",), "NEW_CONNECTOR_REQUIRED", ("magento", "sage"), "INVALIDATED"),
        ValidationOutcome("d", ("quora",), "NEW_CONNECTOR_REQUIRED", ("magento",), "INVALIDATED"),
    ]
    priors = calibrate(outcomes, strength=2)
    validated_class = _opp("PACKAGE_AS_TEMPLATE", ["stripe", "quickbooks"])
    invalidated_class = _opp("NEW_CONNECTOR_REQUIRED", ["magento", "sage"])
    ranked = apply_priors([invalidated_class, validated_class], priors)
    # the class/apps that validated for us is lifted above the one that didn't, regardless of input order
    assert ranked[0][0].opportunity_id == validated_class.opportunity_id
    # reweight, not gate: the down-weighted opportunity still has a positive adjusted score
    assert ranked[-1][1] > 0
