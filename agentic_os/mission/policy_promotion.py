"""Policy promotion contract for shadow-learned decision policies.

This module does not train or choose a policy. It defines the small audit
surface required before a learned or decision-model policy can move from
shadow evidence toward live use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from typing import Mapping

from agentic_os.mission.assessment import ComparabilityStatus
from agentic_os.mission.context_view import content_hash

CONTRACT_VERSION = "policy-promotion/v1"


class PolicyCandidateState(str, Enum):
    DRAFT = "DRAFT"
    SHADOW = "SHADOW"
    EVALUATED = "EVALUATED"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"
    CANARY = "CANARY"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"
    EXPIRED = "EXPIRED"


class PolicyPromotionStatus(str, Enum):
    BLOCK = "BLOCK"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"
    PROMOTE = "PROMOTE"


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


@dataclass(frozen=True)
class PolicyCandidate:
    candidate_id: str
    policy_id: str
    version: str
    parent_policy_id: str
    parent_policy_pin: str
    state: PolicyCandidateState | str
    methodology_id: str
    methodology_hash: str
    action_set_digest: str
    reward_horizon: str
    decision_model_identity: Mapping[str, object]
    created_from_episode_digests: tuple[str, ...]
    created_at: str
    updated_at: str
    approval_ref: str = ""
    rollback_ref: str = ""
    promotion_ref: str = ""
    metrics: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in (
            "candidate_id", "policy_id", "version", "parent_policy_id", "parent_policy_pin",
            "methodology_id", "methodology_hash", "action_set_digest", "reward_horizon",
            "created_at", "updated_at",
        ):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        try:
            state = PolicyCandidateState(_enum_value(self.state))
        except ValueError:
            errors.append(f"unknown policy candidate state {self.state!r}")
            state = None
        if not self.decision_model_identity:
            errors.append("decision_model_identity is required")
        if not self.created_from_episode_digests:
            errors.append("created_from_episode_digests is required")
        if len(set(self.created_from_episode_digests)) != len(self.created_from_episode_digests):
            errors.append("created_from_episode_digests contains duplicates")
        for name, value in self.metrics.items():
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                errors.append(f"metric value for {name!r} must be finite")
        if state in (PolicyCandidateState.PROMOTED, PolicyCandidateState.CANARY) and not self.approval_ref:
            errors.append("approval_ref is required for canary or promoted policies")
        if state is PolicyCandidateState.ROLLED_BACK and not self.rollback_ref:
            errors.append("rollback_ref is required for rolled back policies")
        return tuple(errors)

    def is_valid(self) -> bool:
        return not self.validation_errors()

    def compatibility_payload(self) -> Mapping[str, object]:
        return {
            "policy_id": self.policy_id,
            "parent_policy_id": self.parent_policy_id,
            "methodology_id": self.methodology_id,
            "methodology_hash": self.methodology_hash,
            "action_set_digest": self.action_set_digest,
            "reward_horizon": self.reward_horizon,
            "decision_model_identity": dict(sorted(self.decision_model_identity.items())),
        }

    def compatibility_hash(self) -> str:
        payload = json.dumps(self.compatibility_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "candidate_id": self.candidate_id,
            "policy_id": self.policy_id,
            "version": self.version,
            "parent_policy_id": self.parent_policy_id,
            "parent_policy_pin": self.parent_policy_pin,
            "state": _enum_value(self.state),
            "methodology_id": self.methodology_id,
            "methodology_hash": self.methodology_hash,
            "action_set_digest": self.action_set_digest,
            "reward_horizon": self.reward_horizon,
            "decision_model_identity": dict(sorted(self.decision_model_identity.items())),
            "created_from_episode_digests": list(self.created_from_episode_digests),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approval_ref": self.approval_ref,
            "rollback_ref": self.rollback_ref,
            "promotion_ref": self.promotion_ref,
            "metrics": dict(sorted((k, float(v)) for k, v in self.metrics.items())),
            "metadata": dict(sorted(self.metadata.items())),
            "compatibility_hash": self.compatibility_hash(),
        }

    def digest(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


@dataclass(frozen=True)
class PolicyPromotionEvidence:
    observed_episode_digests: tuple[str, ...]
    verified_outcome_count: int
    effective_sample_size: float
    comparability_status: ComparabilityStatus | str
    policy_violations: int = 0
    duplicate_consequential_writes: int = 0
    protected_slice_regressions: int = 0
    hidden_trial_count: int = 0
    approval_ref: str = ""
    rollback_plan_ref: str = ""
    evidence_ref: str = ""

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.observed_episode_digests:
            errors.append("observed_episode_digests is required")
        if len(set(self.observed_episode_digests)) != len(self.observed_episode_digests):
            errors.append("observed_episode_digests contains duplicates")
        for name in (
            "verified_outcome_count", "policy_violations", "duplicate_consequential_writes",
            "protected_slice_regressions", "hidden_trial_count",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                errors.append(f"{name} must be a non-negative integer")
        if not isinstance(self.effective_sample_size, (int, float)) or not math.isfinite(float(self.effective_sample_size)):
            errors.append("effective_sample_size must be finite")
        elif float(self.effective_sample_size) < 0.0:
            errors.append("effective_sample_size must be non-negative")
        try:
            ComparabilityStatus(_enum_value(self.comparability_status))
        except ValueError:
            errors.append(f"unknown comparability status {self.comparability_status!r}")
        return tuple(errors)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "observed_episode_digests": list(self.observed_episode_digests),
            "verified_outcome_count": self.verified_outcome_count,
            "effective_sample_size": float(self.effective_sample_size),
            "comparability_status": _enum_value(self.comparability_status),
            "policy_violations": self.policy_violations,
            "duplicate_consequential_writes": self.duplicate_consequential_writes,
            "protected_slice_regressions": self.protected_slice_regressions,
            "hidden_trial_count": self.hidden_trial_count,
            "approval_ref": self.approval_ref,
            "rollback_plan_ref": self.rollback_plan_ref,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True)
class PolicyPromotionGate:
    min_verified_outcomes: int
    min_effective_sample_size: float
    require_approval: bool = True
    require_rollback_plan: bool = True
    allow_hidden_trials: bool = False

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not isinstance(self.min_verified_outcomes, int) or self.min_verified_outcomes < 1:
            errors.append("min_verified_outcomes must be a positive integer")
        if (
            not isinstance(self.min_effective_sample_size, (int, float))
            or not math.isfinite(float(self.min_effective_sample_size))
            or float(self.min_effective_sample_size) <= 0.0
        ):
            errors.append("min_effective_sample_size must be positive and finite")
        return tuple(errors)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "min_verified_outcomes": self.min_verified_outcomes,
            "min_effective_sample_size": float(self.min_effective_sample_size),
            "require_approval": self.require_approval,
            "require_rollback_plan": self.require_rollback_plan,
            "allow_hidden_trials": self.allow_hidden_trials,
        }


@dataclass(frozen=True)
class PolicyPromotionDecision:
    status: PolicyPromotionStatus | str
    candidate_digest: str
    candidate_compatibility_hash: str
    reasons: tuple[str, ...]
    gates: Mapping[str, object]

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "status": _enum_value(self.status),
            "candidate_digest": self.candidate_digest,
            "candidate_compatibility_hash": self.candidate_compatibility_hash,
            "reasons": list(self.reasons),
            "gates": self.gates,
        }


def decide_policy_promotion(
    candidate: PolicyCandidate,
    evidence: PolicyPromotionEvidence,
    gate: PolicyPromotionGate,
) -> PolicyPromotionDecision:
    """Derive a promotion decision from explicit evidence and safety gates."""

    blocking = list(candidate.validation_errors() + evidence.validation_errors() + gate.validation_errors())
    review: list[str] = []

    try:
        state = PolicyCandidateState(_enum_value(candidate.state))
    except ValueError:
        state = None
    if state not in (PolicyCandidateState.EVALUATED, PolicyCandidateState.REQUIRE_REVIEW, PolicyCandidateState.CANARY):
        blocking.append("candidate state must be EVALUATED, REQUIRE_REVIEW or CANARY")

    if evidence.verified_outcome_count < gate.min_verified_outcomes:
        blocking.append("verified outcome count below promotion gate")
    if float(evidence.effective_sample_size) < float(gate.min_effective_sample_size):
        blocking.append("effective sample size below promotion gate")
    if _enum_value(evidence.comparability_status) != ComparabilityStatus.COMPARABLE.value:
        blocking.append("comparability status must be COMPARABLE")
    if evidence.policy_violations:
        blocking.append("policy violations block promotion")
    if evidence.duplicate_consequential_writes:
        blocking.append("duplicate consequential writes block promotion")
    if evidence.protected_slice_regressions:
        blocking.append("protected slice regressions block promotion")
    if evidence.hidden_trial_count and not gate.allow_hidden_trials:
        blocking.append("hidden trials block promotion")

    if gate.require_approval and not evidence.approval_ref:
        review.append("authorized approval is required")
    if gate.require_rollback_plan and not evidence.rollback_plan_ref:
        review.append("rollback plan is required")

    if blocking:
        status = PolicyPromotionStatus.BLOCK
        reasons = tuple(blocking)
    elif review:
        status = PolicyPromotionStatus.REQUIRE_REVIEW
        reasons = tuple(review)
    else:
        status = PolicyPromotionStatus.PROMOTE
        reasons = ("all promotion gates passed",)

    return PolicyPromotionDecision(
        status=status,
        candidate_digest=candidate.digest(),
        candidate_compatibility_hash=candidate.compatibility_hash(),
        reasons=reasons,
        gates={
            "gate": gate.canonical_payload(),
            "evidence": evidence.canonical_payload(),
        },
    )


__all__ = [
    "CONTRACT_VERSION",
    "PolicyCandidate",
    "PolicyCandidateState",
    "PolicyPromotionDecision",
    "PolicyPromotionEvidence",
    "PolicyPromotionGate",
    "PolicyPromotionStatus",
    "decide_policy_promotion",
]
