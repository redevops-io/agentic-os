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

__all__ = [
    "Claim", "ClaimStatus",
    "Deal", "DealEvidence",
    "CommitteeRole", "RoleStatus", "CommitteeMember", "BuyingCommittee",
    "ConditionState", "DealCondition", "EvidenceSignal", "NORMALIZED_CONDITIONS", "DEFAULT_MAX_AGE_MS",
    "assess_condition", "derive_state",
]
