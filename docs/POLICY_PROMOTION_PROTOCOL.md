# Policy Promotion Protocol

**Contract version:** `policy-promotion/v1`

This protocol governs how a shadow-learned or decision-model policy candidate may move toward live use. It does not train a model, choose a policy, or grant write authority.

## Lifecycle

Allowed candidate states:

`DRAFT -> SHADOW -> EVALUATED -> REQUIRE_REVIEW -> CANARY -> PROMOTED`

Terminal or corrective states:

`REJECTED`, `ROLLED_BACK`, `EXPIRED`

Only `EVALUATED`, `REQUIRE_REVIEW`, and `CANARY` candidates are eligible for a promotion decision. `PROMOTED` and `CANARY` candidates require an `approval_ref`; `ROLLED_BACK` candidates require a `rollback_ref`.

## Candidate Identity

`PolicyCandidate` records:

- `candidate_id`, `policy_id`, `version`
- `parent_policy_id`, `parent_policy_pin`
- `state`
- `methodology_id`, `methodology_hash`
- `action_set_digest`
- `reward_horizon`
- `decision_model_identity`
- `created_from_episode_digests`
- `created_at`, `updated_at`
- optional `approval_ref`, `rollback_ref`, `promotion_ref`
- `metrics`, `metadata`

The compatibility hash is computed from policy identity, parent policy identity, methodology identity, action-set digest, reward horizon, and decision-model identity. A changed methodology, reward horizon, action set, or model identity must block invalid comparison.

## Evidence

`PolicyPromotionEvidence` records:

- `observed_episode_digests`
- `verified_outcome_count`
- `effective_sample_size`
- `comparability_status`
- `policy_violations`
- `duplicate_consequential_writes`
- `protected_slice_regressions`
- `hidden_trial_count`
- optional `approval_ref`, `rollback_plan_ref`, `evidence_ref`

Promotion evidence must come from independently verified outcomes. Agent self-report, successful API invocation, or unverified readback is not sufficient.

## Gate

`PolicyPromotionGate` declares:

- `min_verified_outcomes`
- `min_effective_sample_size`
- `require_approval`
- `require_rollback_plan`
- `allow_hidden_trials`

## Decision

`decide_policy_promotion` returns:

- `BLOCK` when validation fails, support is insufficient, comparability is not `COMPARABLE`, policy violations exist, duplicate consequential writes occurred, protected slices regressed, or hidden trials are disallowed.
- `REQUIRE_REVIEW` when support and safety gates pass but approval or rollback plan evidence is missing.
- `PROMOTE` only when all support, comparability, approval, and rollback gates pass.

Promotion must never rewrite historical decision episodes. Corrections are represented by a versioned successor, rollback record, or erratum.
