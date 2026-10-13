from agentic_os.mission.assessment import ComparabilityStatus
from agentic_os.mission.policy_promotion import (
    PolicyCandidate,
    PolicyCandidateState,
    PolicyPromotionEvidence,
    PolicyPromotionGate,
    PolicyPromotionStatus,
    decide_policy_promotion,
)


def _candidate(**kwargs) -> PolicyCandidate:
    base = dict(
        candidate_id="candidate-1",
        policy_id="crossapp-routing-policy",
        version="2026.10-shadow-1",
        parent_policy_id="crossapp-routing-policy",
        parent_policy_pin="crossapp-routing-policy@v1",
        state=PolicyCandidateState.EVALUATED,
        methodology_id="apps-e2e-raaal-methodology",
        methodology_hash="sha256:methodology",
        action_set_digest="sha256:actions",
        reward_horizon="P30D",
        decision_model_identity={"provider_id": "small-model", "model_pin": "small-model@sha256:abc"},
        created_from_episode_digests=("sha256:ep1", "sha256:ep2"),
        created_at="2026-10-12T00:00:00Z",
        updated_at="2026-10-12T00:00:00Z",
        metrics={"utility_delta": 0.12},
    )
    base.update(kwargs)
    return PolicyCandidate(**base)


def _evidence(**kwargs) -> PolicyPromotionEvidence:
    base = dict(
        observed_episode_digests=("sha256:ep1", "sha256:ep2", "sha256:ep3"),
        verified_outcome_count=3,
        effective_sample_size=3.0,
        comparability_status=ComparabilityStatus.COMPARABLE,
        approval_ref="approval:42",
        rollback_plan_ref="rollback:42",
        evidence_ref="assessment:crossapp",
    )
    base.update(kwargs)
    return PolicyPromotionEvidence(**base)


def _gate(**kwargs) -> PolicyPromotionGate:
    base = dict(min_verified_outcomes=3, min_effective_sample_size=3.0)
    base.update(kwargs)
    return PolicyPromotionGate(**base)


def test_policy_candidate_identity_and_compatibility_hash_are_stable():
    candidate = _candidate(decision_model_identity={"model_pin": "small-model@sha256:abc", "provider_id": "small-model"})
    same = _candidate(decision_model_identity={"provider_id": "small-model", "model_pin": "small-model@sha256:abc"})

    assert candidate.validation_errors() == ()
    assert candidate.compatibility_hash() == same.compatibility_hash()
    assert candidate.digest() == same.digest()
    assert candidate.canonical_payload()["contract_version"] == "policy-promotion/v1"


def test_candidate_validation_keeps_lineage_and_approval_explicit():
    promoted_without_approval = _candidate(state=PolicyCandidateState.PROMOTED, approval_ref="")
    rolled_back_without_ref = _candidate(state=PolicyCandidateState.ROLLED_BACK, rollback_ref="")
    bad = _candidate(
        created_from_episode_digests=("sha256:ep1", "sha256:ep1"),
        metrics={"utility_delta": float("nan")},
    )

    assert promoted_without_approval.validation_errors() == (
        "approval_ref is required for canary or promoted policies",
    )
    assert rolled_back_without_ref.validation_errors() == (
        "rollback_ref is required for rolled back policies",
    )
    assert bad.validation_errors() == (
        "created_from_episode_digests contains duplicates",
        "metric value for 'utility_delta' must be finite",
    )


def test_promotion_blocks_on_insufficient_unverified_or_unsafe_evidence():
    decision = decide_policy_promotion(
        _candidate(),
        _evidence(
            verified_outcome_count=1,
            effective_sample_size=1.2,
            policy_violations=1,
            duplicate_consequential_writes=1,
            hidden_trial_count=1,
        ),
        _gate(min_verified_outcomes=3, min_effective_sample_size=3.0),
    )

    assert decision.status is PolicyPromotionStatus.BLOCK
    assert decision.reasons == (
        "verified outcome count below promotion gate",
        "effective sample size below promotion gate",
        "policy violations block promotion",
        "duplicate consequential writes block promotion",
        "hidden trials block promotion",
    )


def test_promotion_requires_review_for_missing_authorization_or_rollback_plan():
    decision = decide_policy_promotion(
        _candidate(),
        _evidence(approval_ref="", rollback_plan_ref=""),
        _gate(),
    )

    assert decision.status is PolicyPromotionStatus.REQUIRE_REVIEW
    assert decision.reasons == (
        "authorized approval is required",
        "rollback plan is required",
    )


def test_promotion_passes_only_after_support_comparability_approval_and_rollback():
    decision = decide_policy_promotion(_candidate(), _evidence(), _gate())

    assert decision.status is PolicyPromotionStatus.PROMOTE
    assert decision.reasons == ("all promotion gates passed",)
    assert decision.canonical_payload()["candidate_compatibility_hash"]


def test_incompatible_comparison_blocks_even_with_good_metrics():
    decision = decide_policy_promotion(
        _candidate(),
        _evidence(comparability_status=ComparabilityStatus.NOT_COMPARABLE),
        _gate(),
    )

    assert decision.status is PolicyPromotionStatus.BLOCK
    assert decision.reasons == ("comparability status must be COMPARABLE",)
