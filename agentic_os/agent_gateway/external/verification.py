"""Phase 6 — evidence + verification (plan §11 Phase 6, §6).

The core rule: **a provider's success claim is evidence, not truth.** Verification separates what the
provider asserts (``AgentTaskResult.provider_claimed_success``) from what ReDevOps can actually observe,
and it may ABSTAIN rather than pass when evidence is insufficient. It also re-binds the result to the
exact authorized request via the intent digest — a result for a different request never verifies.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional, Protocol, Tuple

from .contracts import AgentTaskRef, AgentTaskResult, TaskState


class VerificationStatus(str, enum.Enum):
    VERIFIED = "verified"       # observed evidence supports the claimed outcome
    REFUTED = "refuted"         # claim contradicted by observation (e.g. success with no artifact)
    ABSTAINED = "abstained"     # not enough evidence to affirm or refute — do NOT treat as success
    NOT_APPLICABLE = "n/a"      # task did not claim completion (still running / cancelled / failed)
    UNKNOWN = "unknown"         # the independent probe could not answer: the effect may or may not have happened


class ProbeResult(str, enum.Enum):
    FOUND = "found"             # the effect is observed in a system the provider's own reply does not control
    ABSENT = "absent"           # a reliable read says the effect is not there
    ERROR = "error"             # the read failed or timed out: no answer (never treated as FOUND or ABSENT)


class PostconditionProbe(Protocol):
    """An INDEPENDENT read of the postcondition (message-status API, ledger lookup, file open and parse).
    It must not trust the provider's task result: it asks the system of record. ``ERROR`` means no answer."""

    def probe(self, ref: AgentTaskRef, result: AgentTaskResult) -> ProbeResult: ...


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


def verify_result(request_digest: str, ref: AgentTaskRef, result: AgentTaskResult, *,
                  probe: Optional[PostconditionProbe] = None,
                  require_independent: bool = False) -> VerificationOutcome:
    """Verify a provider result against the authorized request. Fail-closed and honest about uncertainty.

    With a ``probe`` the final answer comes from an independent read (FOUND → VERIFIED, ABSENT → REFUTED,
    ERROR → UNKNOWN). ``require_independent`` (the default for any non-simulated adapter) forbids minting
    VERIFIED from the provider's own artifact refs alone: without a probe it abstains."""
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

    # 5. Artifact present: an independent probe (when configured) decides; the provider's refs alone do not.
    if probe is not None:
        try:
            seen = probe.probe(ref, result)
        except Exception:                                   # a failing probe is "no answer", never "found"
            seen = ProbeResult.ERROR
        if seen is ProbeResult.FOUND:
            return VerificationOutcome(VerificationStatus.VERIFIED, "effect independently observed",
                                       request_digest=request_digest, result_task_ref=result.provider_task_ref,
                                       evidence_refs=result.evidence_refs + result.artifact_refs)
        if seen is ProbeResult.ABSENT:
            return VerificationOutcome(VerificationStatus.REFUTED,
                                       "provider reported an artifact but the independent check found no such effect",
                                       request_digest=request_digest, result_task_ref=result.provider_task_ref)
        return VerificationOutcome(VerificationStatus.UNKNOWN,
                                   "the independent check could not answer; the effect may have happened",
                                   request_digest=request_digest, result_task_ref=result.provider_task_ref,
                                   evidence_refs=result.evidence_refs)
    if require_independent:
        return VerificationOutcome(VerificationStatus.ABSTAINED,
                                   "no independent probe configured; the provider's own artifact is not proof",
                                   request_digest=request_digest, result_task_ref=result.provider_task_ref,
                                   evidence_refs=result.evidence_refs)
    return VerificationOutcome(VerificationStatus.VERIFIED, "artifact and evidence observed",
                               request_digest=request_digest, result_task_ref=result.provider_task_ref,
                               evidence_refs=result.evidence_refs + result.artifact_refs)
