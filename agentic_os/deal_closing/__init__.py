"""Deal Closing Intelligence & Execution (Phase 1) — closing as a governed cross-system decision problem.

This package treats closing a deal not as methodology coaching but as a DECISION-and-EXECUTION problem over
verified cross-system state: observe the complete deal → identify missing conditions and blockers → select a
strategy → propose executable actions → obtain authority → execute → VERIFY what actually changed → learn which
interventions move deals. It reuses the existing substrate (Mission Runtime, evidence/provenance, entity
resolution, Revenue Leakage, Quote Feasibility, approval gates, credential leasing, outcome learning) rather than
building a second sales-agent runtime.

Phase 1 ships the state substrate: the reported-vs-verified :class:`Deal`, its :class:`DealEvidence`, the
evidence-backed :class:`BuyingCommittee`, and the six-valued :class:`DealCondition`. Methodology (Phase 2), the
blocker/hypothesis engine (Phase 3), the Close-Plan→Mission-DAG compiler (Phase 4) and outcome learning (Phase 7)
build on these.
"""
from .contracts import (
    BuyingCommittee, Claim, ClaimStatus, CommitteeMember, CommitteeRole, Deal, DealEvidence, RoleStatus,
)
from .conditions import (
    ConditionState, DealCondition, DEFAULT_MAX_AGE_MS, EvidenceSignal, NORMALIZED_CONDITIONS,
    assess_condition, derive_state,
)
from .methodology import (
    ClosingMethodology, ConditionSpec, MethodologyReadiness, assess_methodology, compile_methodology,
    states_from_conditions, validate_methodology,
)
from .library import builtin_names, get_methodology, meddicc, spiced
from .blockers import Blocker, BlockerType, InterventionKind, infer_blockers
from .hypotheses import ClosingHypothesis, hypothesis_for, plan_hypotheses
from .autonomy import ActionDisposition, AutonomyLevel, disposition, risk_tier_for
from .candidates import (
    CandidateAction, ScoredAction, best_action, candidates_from_hypothesis, no_action, score_actions,
    to_priority_candidate,
)
from .close_plan import (
    ClosePlanMissionSpec, DealClosePlan, PlannedStep, compile_close_plan, handoff, to_mission_spec,
)
from .learn import (
    AttributionRung, ClosingInterventionOutcome, ClosingPriors, InterventionLedger, apply_priors,
    attribution_strength, calibrate, classify_rung,
)

__all__ = [
    "Claim", "ClaimStatus",
    "Deal", "DealEvidence",
    "CommitteeRole", "RoleStatus", "CommitteeMember", "BuyingCommittee",
    "ConditionState", "DealCondition", "EvidenceSignal", "NORMALIZED_CONDITIONS", "DEFAULT_MAX_AGE_MS",
    "assess_condition", "derive_state",
    "ClosingMethodology", "ConditionSpec", "MethodologyReadiness",
    "compile_methodology", "validate_methodology", "assess_methodology", "states_from_conditions",
    "meddicc", "spiced", "get_methodology", "builtin_names",
    "Blocker", "BlockerType", "InterventionKind", "infer_blockers",
    "ClosingHypothesis", "hypothesis_for", "plan_hypotheses",
    "AutonomyLevel", "ActionDisposition", "disposition", "risk_tier_for",
    "CandidateAction", "ScoredAction", "no_action", "candidates_from_hypothesis",
    "to_priority_candidate", "score_actions", "best_action",
    "DealClosePlan", "PlannedStep", "compile_close_plan",
    "ClosePlanMissionSpec", "to_mission_spec", "handoff",
    "AttributionRung", "ClosingInterventionOutcome", "classify_rung", "attribution_strength",
    "ClosingPriors", "calibrate", "apply_priors", "InterventionLedger",
]
