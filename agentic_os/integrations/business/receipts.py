"""Governed connector write → canonical ActionReceipt + independent verification (plan §22, Strength 5).

The Integration Plane runner (:mod:`agentic_os.integrations.execution`) executes each write under a
``GovernedEnvelope`` and reconciles it by re-observing the provider object. This module turns that outcome
into the platform's canonical :class:`agentic_os.projects.contracts.ActionReceipt` and a separate
``VerificationState`` — so a connector write plugs into the same receipt/verification the rest of the
platform (Projects, Mission Runtime) uses, instead of a plane-local result type.

The core rule (Strength 5): **provider success is not real-world success.** A write the provider accepted
but which re-observation could not confirm is HELD, never SUCCEEDED. Where re-observation is impossible
the state is UNKNOWN — surfaced honestly, not silently treated as success.
"""
from __future__ import annotations

import enum
from typing import Optional, Tuple


class VerificationState(str, enum.Enum):
    VERIFIED = "verified"           # write succeeded AND re-observation confirmed the object
    REFUTED = "refuted"             # write reported success BUT re-observation did not find it
    UNKNOWN = "unknown"             # write succeeded, but the object is not observable → cannot confirm
    NOT_APPLICABLE = "n/a"          # the write itself failed → nothing to verify

    @property
    def is_success(self) -> bool:
        return self is VerificationState.VERIFIED


def verify_step(ok: bool, reconciled: Optional[bool]) -> VerificationState:
    """Map an execution outcome (ok, reconciled=re-observed?) to a verification state. ``reconciled`` is
    True (found on reread), False (not found), or None (unobservable)."""
    if not ok:
        return VerificationState.NOT_APPLICABLE
    if reconciled is True:
        return VerificationState.VERIFIED
    if reconciled is False:
        return VerificationState.REFUTED
    return VerificationState.UNKNOWN


def _receipt_status(ok: bool, v: VerificationState) -> str:
    # ActionReceipt.status vocabulary is SUCCEEDED | FAILED | HELD.
    if not ok:
        return "FAILED"
    if v is VerificationState.VERIFIED:
        return "SUCCEEDED"
    return "HELD"                   # provider ok but unverified/refuted → not claimed as success


def to_action_receipt(*, capability: str, provider: str, ok: bool, provider_object_id: str = "",
                      reconciled: Optional[bool] = None, decision_id: str = "", artifact_id: str = "",
                      external_url: str = "", error: str = "") -> Tuple["object", VerificationState]:
    """Build the canonical ActionReceipt + the verification state for one governed connector write.

    Returns ``(ActionReceipt, VerificationState)``. ActionReceipt is imported lazily so the Integration
    Plane does not hard-depend on the projects package (which pulls discovery_runtime)."""
    from agentic_os.projects.contracts import ActionReceipt  # noqa: PLC0415

    v = verify_step(ok, reconciled)
    status = _receipt_status(ok, v)
    note = error or ("" if v in (VerificationState.VERIFIED, VerificationState.NOT_APPLICABLE)
                     else f"verification: {v.value}")
    receipt = ActionReceipt(
        artifact_id=artifact_id or (provider_object_id or f"{provider}:{capability}"),
        capability=capability, provider=provider, status=status,
        external_id=provider_object_id, external_url=external_url, error=note, decision_id=decision_id)
    return receipt, v


def receipt_for_step(step: "object", *, decision_id: str = "",
                     artifact_id: str = "") -> Tuple["object", VerificationState]:
    """Convenience over :func:`to_action_receipt` for an Integration-Plane ``StepRun``
    (``agentic_os.integrations.execution.StepRun``: capability/provider/ok/provider_object_id/reconciled/error)."""
    return to_action_receipt(
        capability=getattr(step, "capability", ""), provider=getattr(step, "provider", ""),
        ok=bool(getattr(step, "ok", False)), provider_object_id=str(getattr(step, "provider_object_id", "")),
        reconciled=getattr(step, "reconciled", None), decision_id=decision_id, artifact_id=artifact_id,
        error=str(getattr(step, "error", "")))
