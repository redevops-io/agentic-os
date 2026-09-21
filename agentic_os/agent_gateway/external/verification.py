"""Phase 6 — evidence + verification (plan §11 Phase 6, §6).

The core rule: **a provider's success claim is evidence, not truth.** Verification separates what the
provider asserts (``AgentTaskResult.provider_claimed_success``) from what ReDevOps can actually observe,
and it may ABSTAIN rather than pass when evidence is insufficient. It also re-binds the result to the
exact authorized request via the intent digest — a result for a different request never verifies.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Tuple

from .contracts import AgentTaskRef, AgentTaskResult, TaskState


class VerificationStatus(str, enum.Enum):
    VERIFIED = "verified"       # observed evidence supports the claimed outcome
    REFUTED = "refuted"         # claim contradicted by observation (e.g. success with no artifact)
    ABSTAINED = "abstained"     # not enough evidence to affirm or refute — do NOT treat as success
    NOT_APPLICABLE = "n/a"      # task did not claim completion (still running / cancelled / failed)


@dataclass(frozen=True)
class VerificationOutcome:
    status: VerificationStatus
    reason: str
    request_digest: str = ""
    result_task_ref: str = ""
    evidence_refs: Tuple[str, ...] = ()

    @property
    def is_success(self) -> bool:
        """Only an affirmatively VERIFIED outcome counts as a real success. Abstention is not success."""
        return self.status is VerificationStatus.VERIFIED


def verify_result(request_digest: str, ref: AgentTaskRef, result: AgentTaskResult) -> VerificationOutcome:
    """Verify a provider result against the authorized request. Fail-closed and honest about uncertainty."""
    # 1. Re-bind: the result must belong to the exact authorized request (digest carried on the ref).
    if ref.intent_digest and request_digest and ref.intent_digest != request_digest:
        return VerificationOutcome(VerificationStatus.REFUTED,
                                   "result is bound to a different authorized request",
                                   request_digest=request_digest, result_task_ref=result.provider_task_ref)

    # 2. Non-completion states are not verification failures — they simply don't affirm success.
    if result.state is not TaskState.SUCCEEDED:
        return VerificationOutcome(VerificationStatus.NOT_APPLICABLE,
                                   f"task state is {result.state.value}, not SUCCEEDED",
                                   request_digest=request_digest, result_task_ref=result.provider_task_ref)

    # 3. A claimed success with no artifact AND no evidence is refuted (the "provider lies" case).
    if result.provider_claimed_success and not result.artifact_refs and not result.evidence_refs:
        return VerificationOutcome(VerificationStatus.REFUTED,
                                   "provider claimed success but returned no artifact or evidence",
                                   request_digest=request_digest, result_task_ref=result.provider_task_ref)

    # 4. Some evidence but not a strong artifact → abstain (uncertainty preserved, not auto-passed).
    if not result.artifact_refs:
        return VerificationOutcome(VerificationStatus.ABSTAINED,
                                   "evidence present but no verifiable artifact; abstaining",
                                   request_digest=request_digest, result_task_ref=result.provider_task_ref,
                                   evidence_refs=result.evidence_refs)

    # 5. Artifact + evidence present and the state is SUCCEEDED → verified.
    return VerificationOutcome(VerificationStatus.VERIFIED, "artifact and evidence observed",
                               request_digest=request_digest, result_task_ref=result.provider_task_ref,
                               evidence_refs=result.evidence_refs + result.artifact_refs)
