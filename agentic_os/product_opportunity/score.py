"""Decomposed opportunity score + capability leverage (Phase 5, plan §13/§21).

A ReDevOps-specific score, DECOMPOSED (never one opaque number): pain severity × recurrence × economic impact
× cross-app friction × agentic fit × reusability × evidence strength × commercial fit ÷ implementation cost.
Implementation cost comes straight from the Phase-4 classification (packaging is cheap; a new connector or new
domain logic is dear), so "already built, just unpackaged" ranks above "needs a new connector" at equal demand.

Reuses the cross-app ranker rather than inventing a second one: ``to_priority_candidate`` maps an opportunity
onto ``priority_engine.InterventionCandidate`` so the existing ``decide`` gives the ACT / REQUEST_APPROVAL /
DEFER / ABSTAIN call and cross-surface ranking. ``capability_leverage`` is the second roadmap signal — a
capability many high-scoring opportunities depend on outranks a one-off (plan §21).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import prod
from typing import Dict, List, Sequence, Tuple

from ..priority_engine import InterventionCandidate, RiskTier
from .capability_map import CapabilityCoverage
from .cluster import WorkflowCluster

_IMPL_COST = {"ALREADY_SUPPORTED": 0.10, "PACKAGE_AS_TEMPLATE": 0.25, "SMALL_INTEGRATION_GAP": 0.45,
              "NEW_DOMAIN_LOGIC_REQUIRED": 0.60, "NEW_CONNECTOR_REQUIRED": 0.75,
              "NEW_RUNTIME_PRIMITIVE_REQUIRED": 0.90, "OUT_OF_SCOPE": 1.0}
_AGENTIC = {"low": 0.3, "medium": 0.6, "high": 1.0}
_CONSEQUENCE_WEIGHT = {"lost_sales": 0.9, "churn": 0.9, "margin_pressure": 0.85, "compliance_risk": 0.8,
                       "time_cost": 0.6, "errors": 0.5}
_STATUSES = ("OBSERVED", "CLUSTERED", "CANDIDATE", "VALIDATING", "PLANNED", "IMPLEMENTING", "SHIPPED",
             "MEASURING", "LEARNED", "REJECTED")


@dataclass(frozen=True)
class OpportunityScore:
    pain_severity: float
    recurrence: float
    economic_impact: float
    cross_app_friction: float
    agentic_fit: float
    reusability: float
    evidence_strength: float
    commercial_fit: float
    implementation_cost: float
    composite: float                     # geometric mean of the 8 benefits ÷ implementation cost

    def factors(self) -> Dict[str, float]:
        return {k: getattr(self, k) for k in (
            "pain_severity", "recurrence", "economic_impact", "cross_app_friction", "agentic_fit",
            "reusability", "evidence_strength", "commercial_fit", "implementation_cost")}


def score_opportunity(cluster: WorkflowCluster, coverage: CapabilityCoverage) -> OpportunityScore:
    wp = cluster.workflow_pain
    dims = set(wp.pain_dimensions)
    pain_severity = min(1.0, 0.4 + 0.15 * len(dims))
    recurrence = min(1.0, cluster.independent_evidence_count / 5.0)
    economic_impact = max([_CONSEQUENCE_WEIGHT.get(c, 0.0) for c in wp.business_consequences] + [0.3])
    cross_app_friction = 1.0 if "cross_app" in dims else 0.4
    agentic_fit = _AGENTIC.get(wp.agentic_fit, 0.6)
    reusability = 0.9 if coverage.classification in ("ALREADY_SUPPORTED", "PACKAGE_AS_TEMPLATE") else 0.6
    evidence_strength = min(1.0, 0.3 + 0.12 * cluster.independent_authors + 0.1 * len(cluster.sources))
    commercial_fit = round(0.5 + 0.5 * coverage.coverage_percent, 4)
    impl_cost = _IMPL_COST.get(coverage.classification, 0.6)

    benefits = [pain_severity, recurrence, economic_impact, cross_app_friction, agentic_fit, reusability,
                evidence_strength, commercial_fit]
    geo = prod(benefits) ** (1.0 / len(benefits))
    composite = round(geo / impl_cost, 4)
    return OpportunityScore(
        pain_severity=round(pain_severity, 4), recurrence=round(recurrence, 4),
        economic_impact=round(economic_impact, 4), cross_app_friction=cross_app_friction,
        agentic_fit=agentic_fit, reusability=reusability, evidence_strength=round(evidence_strength, 4),
        commercial_fit=commercial_fit, implementation_cost=impl_cost, composite=composite)


@dataclass(frozen=True)
class ProductOpportunity:
    opportunity_id: str
    title: str
    workflow_pain_id: str
    applications: Tuple[str, ...]
    evidence_count: int
    independent_evidence_count: int
    score: OpportunityScore
    coverage: CapabilityCoverage
    status: str = "CANDIDATE"


def build_opportunity(cluster: WorkflowCluster, coverage: CapabilityCoverage) -> ProductOpportunity:
    score = score_opportunity(cluster, coverage)
    return ProductOpportunity(
        opportunity_id="opp_" + cluster.signature.replace("|", "_").replace(":", "_"),
        title=f"{cluster.signature} ({coverage.classification})", workflow_pain_id=cluster.workflow_pain.name,
        applications=cluster.applications, evidence_count=cluster.raw_mentions,
        independent_evidence_count=cluster.independent_evidence_count, score=score, coverage=coverage,
        status="CANDIDATE")


def to_priority_candidate(opp: ProductOpportunity) -> InterventionCandidate:
    """Map an opportunity onto the cross-app ranker so priority_engine.decide gives ACT/ABSTAIN + ranking —
    confidence (is it real) kept separate from expected_value (upside), per §18."""
    s = opp.score
    return InterventionCandidate(
        source_app="product_opportunity", subject=opp.title, proposed_action="validate_opportunity",
        expected_value=round(s.composite, 4), confidence=s.evidence_strength,
        urgency=s.recurrence, execution_cost=s.implementation_cost, attention_cost=0.3,
        risk_tier=RiskTier.READ, reversibility=1.0, information_value=0.6,
        required_capabilities=opp.applications, action_kind="validate_opportunity")


def capability_leverage(opportunities: Sequence[ProductOpportunity]) -> Dict[str, float]:
    """Σ opportunity composite × per-capability dependency across opportunities — the second roadmap signal.
    A connector many high-scoring opportunities need outranks a one-off even if no single opportunity dominates."""
    leverage: Dict[str, float] = {}
    for opp in opportunities:
        apps = opp.applications
        if not apps:
            continue
        dependency = 1.0 / len(apps)
        for app in apps:
            leverage[app] = round(leverage.get(app, 0.0) + opp.score.composite * dependency, 4)
    return dict(sorted(leverage.items(), key=lambda kv: kv[1], reverse=True))
