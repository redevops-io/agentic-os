"""Product Opportunity Intelligence (Phase 1) — discover broken cross-app workflows from public operator pain.

The unit of discovery is a BROKEN WORKFLOW, not a startup idea: a complaint becomes evidence → a reconstructed
workflow → (later) a recurring pain → a capability gap → a validated product. This package ships the evidence
schema (``PainObservation``/``WorkflowPain``), a versioned search/extraction vocabulary, and the deterministic
"broken cross-app workflow" extractor over a social observation. Clustering, capability mapping, scoring,
sweeps, validation and outcome-learning are later phases; live source clients are fake-until-credentialed and
consume the existing ``agent_gateway.social`` provider seam (official-API / permitted access only).
"""
from .contracts import AUTOMATION_FIT, SOURCES, PainObservation, WorkflowPain
from .capability_map import (
    CapabilityCatalog, CapabilityCoverage, WorkflowTemplate, default_catalog, map_coverage,
)
from .cluster import WorkflowCluster, cluster_pains, workflow_signature
from .score import (
    OpportunityScore, ProductOpportunity, build_opportunity, capability_leverage, score_opportunity,
    to_priority_candidate,
)
from .sweep import SweepChange, SweepResult, diff_sweeps, render_brief, run_sweep
from .learn import DiscoveryPriors, ValidationOutcome, apply_priors, calibrate, result_from_usage
from .validate import ValidationError, ValidationMission, approve, plan_validation, reject
from .dedup import IndependenceReport, assess_independence
from .extract import extract_pain
from .vocabulary import (
    APPLICATION_VOCAB, DEFAULT_UNIVERSE, PAIN_MARKERS, WORKFLOW_VERBS, SearchUniverse,
)

__all__ = [
    "PainObservation", "WorkflowPain", "SOURCES", "AUTOMATION_FIT",
    "extract_pain",
    "assess_independence", "IndependenceReport",
    "cluster_pains", "WorkflowCluster", "workflow_signature",
    "map_coverage", "CapabilityCoverage", "CapabilityCatalog", "WorkflowTemplate", "default_catalog",
    "score_opportunity", "OpportunityScore", "build_opportunity", "ProductOpportunity",
    "to_priority_candidate", "capability_leverage",
    "run_sweep", "SweepResult", "diff_sweeps", "SweepChange", "render_brief",
    "plan_validation", "ValidationMission", "approve", "reject", "ValidationError",
    "ValidationOutcome", "result_from_usage", "calibrate", "DiscoveryPriors", "apply_priors",
    "SearchUniverse", "DEFAULT_UNIVERSE", "APPLICATION_VOCAB", "WORKFLOW_VERBS", "PAIN_MARKERS",
]
