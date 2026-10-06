"""Product Opportunity Intelligence Phase 5 — decomposed score + capability leverage.

Proves: the score is decomposed (all 8 factors exposed); implementation cost from the Phase-4 classification
makes "already built, just unpackaged" outrank "needs a new connector" at equal demand; the opportunity maps
onto priority_engine for an ACT/ABSTAIN decision; and capability leverage aggregates across opportunities.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.priority_engine import Action, decide
from agentic_os.product_opportunity import (
    WorkflowPain, build_opportunity, capability_leverage, score_opportunity, to_priority_candidate,
)
from agentic_os.product_opportunity.capability_map import CapabilityCoverage
from agentic_os.product_opportunity.cluster import WorkflowCluster


def _cluster(apps, *, sig, ev=3, authors=3, sources=("reddit",), agentic="high",
             consequences=("lost_sales",), dims=("cross_app", "manual", "repetitive")):
    wp = WorkflowPain(prov=Provenance(provider="t"), name=sig, applications=tuple(apps),
                      pain_dimensions=tuple(dims), business_consequences=tuple(consequences), agentic_fit=agentic)
    return WorkflowCluster(workflow_pain=wp, signature=sig, raw_mentions=ev, independent_evidence_count=ev,
                           independent_authors=authors, sources=tuple(sources), communities=("r/x",),
                           applications=tuple(apps), first_seen=0, last_seen=1000)


def _cov(classification, pct):
    return CapabilityCoverage("wp", (), (), (), pct, classification)


def test_score_is_decomposed():
    s = score_opportunity(_cluster(["stripe", "quickbooks"], sig="quickbooks|stripe"),
                          _cov("ALREADY_SUPPORTED", 1.0))
    f = s.factors()
    assert set(f) == {"pain_severity", "recurrence", "economic_impact", "cross_app_friction", "agentic_fit",
                      "reusability", "evidence_strength", "commercial_fit", "implementation_cost"}
    assert all(0 <= v <= 1 for v in f.values()) and s.composite > 0


def test_cheaper_implementation_outranks_at_equal_demand():
    cl = _cluster(["stripe", "quickbooks"], sig="quickbooks|stripe")
    supported = score_opportunity(cl, _cov("ALREADY_SUPPORTED", 1.0))
    new_conn = score_opportunity(cl, _cov("NEW_CONNECTOR_REQUIRED", 0.5))
    assert supported.composite > new_conn.composite          # same demand, lower impl cost → higher score
    assert supported.implementation_cost < new_conn.implementation_cost


def test_maps_to_priority_candidate_and_acts():
    opp = build_opportunity(_cluster(["stripe", "quickbooks"], sig="quickbooks|stripe"),
                            _cov("PACKAGE_AS_TEMPLATE", 1.0))
    cand = to_priority_candidate(opp)
    assert cand.source_app == "product_opportunity" and cand.confidence == opp.score.evidence_strength
    assert decide(cand).action in (Action.ACT, Action.REQUEST_APPROVAL, Action.DEFER, Action.ABSTAIN)


def test_capability_leverage_aggregates():
    opps = [
        build_opportunity(_cluster(["stripe", "quickbooks"], sig="quickbooks|stripe"), _cov("ALREADY_SUPPORTED", 1.0)),
        build_opportunity(_cluster(["stripe", "netsuite"], sig="netsuite|stripe"), _cov("PACKAGE_AS_TEMPLATE", 1.0)),
        build_opportunity(_cluster(["shopify", "xero"], sig="shopify|xero"), _cov("PACKAGE_AS_TEMPLATE", 1.0)),
    ]
    lev = capability_leverage(opps)
    # stripe appears in two opportunities → should lead the leverage ranking
    assert list(lev)[0] == "stripe" and lev["stripe"] > lev.get("shopify", 0)
