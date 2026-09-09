"""Outreach acceptance Mission — the second P0 acceptance case, complementary to the refund
harness (``acceptance.py``).

Where the refund case proves *governed execution of an existing business operation*, this
proves a different property: **multi-system synthesis → artifact creation → external-system
configuration → a consequential boundary**:

    goal → context → generate copy (Claude) → generate asset (fal.ai, OPTIONAL)
         → configure sequence + enroll recipient (a provider)
         → ACTIVATE (the consequential boundary) → observe → verify delivery

The logical workflow is **provider-independent**. The interesting bit is the activation
boundary: whether it can be automated is a **physical capability result** the harness reads
from the provider (``provider.activation()``), not a hardcoded "this provider needs a human."
Apollo's activation is UI-only, so its adapter advertises ``automatable=False`` and the Mission
**durably pauses** (PROVIDER_UI_REQUIRED); another provider might support API activation, and
then *governance* decides ALLOW vs REQUIRE_REVIEW. Either way the Mission got as far as its
authority/capability allowed and left the consequential operation pending — that is a correct
outcome, not a failure.

The committed harness includes the **post-gate half**: after the human acts, ``resume_outreach``
observes the provider, waits for the send, and produces an :class:`ExecutionReceipt` — giving a
durable *pause → external human action → observation → resume → verification* Mission Runtime test.

The creative asset is **optional** (``copy_required=True``, ``creative_asset`` optional), so the
generic outreach workflow doesn't test an artificial dependency that ordinary cold email lacks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple


# ── logical step keys (portable — no provider names) ─────────────────────────────
PREPARE_OUTREACH = "PREPARE_OUTREACH"
GENERATE_COPY = "GENERATE_COPY"
GENERATE_ASSET = "GENERATE_ASSET"
CONFIGURE_SEQUENCE = "CONFIGURE_SEQUENCE"
ENROLL_RECIPIENT = "ENROLL_RECIPIENT"
ACTIVATE_SEQUENCE = "ACTIVATE_SEQUENCE"
OBSERVE_SEND = "OBSERVE_SEND"
VERIFY_DELIVERY = "VERIFY_DELIVERY"


class StepStatus(str, Enum):
    DONE = "done"                  # an automated step completed
    SKIPPED = "skipped"            # an optional step wasn't requested (e.g. no creative asset)
    PENDING_HUMAN = "pending_human"  # halted at the consequential boundary (provider-UI or governance)
    OBSERVING = "observing"        # sent-but-not-yet-delivered — verification still open
    VERIFIED = "verified"          # delivery confirmed with a provider receipt
    REJECTED = "rejected"          # a human declined at the gate (a correct outcome)
    REFUSED = "refused"            # an unexpected failure (the only status that fails the gate)


@dataclass(frozen=True)
class OutreachRequest:
    goal: str
    recipient: str
    company: str = ""
    campaign_intent: str = ""
    creative_asset: bool = False   # optional — ordinary cold email doesn't require it


@dataclass(frozen=True)
class Copy:
    subject: str
    body_html: str


@dataclass(frozen=True)
class Asset:
    kind: str                      # "image" | "html" | …
    ref: str                       # a path / url / id
    summary: str = ""


@dataclass(frozen=True)
class ActivationCapability:
    """The physical capability result for activating/sending — read from the provider, so the
    logical Mission stays portable. ``automatable=False`` means the Runtime cannot perform it
    (e.g. Apollo activation is provider-UI-only)."""

    automatable: bool = True
    human_required: bool = False
    execution_strategy: str = "api"   # "api" | "provider_ui"
    detail: str = ""


@dataclass(frozen=True)
class ExecutionReceipt:
    """Durable proof a send happened — a provider message/activity id + observed status."""

    provider: str
    object_id: str
    status: str
    observed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "object_id": self.object_id,
                "status": self.status, "observed_at": self.observed_at}


@dataclass(frozen=True)
class Step:
    key: str
    status: StepStatus
    automated: bool
    detail: str = ""
    refs: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "status": self.status.value, "automated": self.automated,
                "detail": self.detail, "refs": list(self.refs)}


class OutreachProvider(Protocol):
    """The physical seam. Any object with these methods (e.g. a wrapper over the Apollo
    connector adapter) satisfies it; tests use a fake."""

    def configure_sequence(self, *, subject: str, body_html: str) -> Any: ...   # -> .ok/.provider_object_id
    def enroll(self, *, recipient: str) -> Any: ...                             # -> .ok/.provider_object_id
    def activation(self) -> ActivationCapability: ...
    def activate(self) -> Any: ...                                             # -> .ok  (only if automatable)
    def observe(self, *, recipient: str) -> Any: ...                            # -> .status/.delivered


CopyFn = Callable[[OutreachRequest], Optional[Copy]]
AssetFn = Callable[[OutreachRequest], Optional[Asset]]


@dataclass(frozen=True)
class OutreachReport:
    request: OutreachRequest
    steps: Tuple[Step, ...]
    pending: bool = False
    pending_reason: str = ""       # "provider_ui" | "governance" | ""
    receipt: Optional[ExecutionReceipt] = None
    _resume: Optional[Dict[str, Any]] = None

    @property
    def refused(self) -> Tuple[Step, ...]:
        return tuple(s for s in self.steps if s.status is StepStatus.REFUSED)

    @property
    def verified(self) -> bool:
        return any(s.key == VERIFY_DELIVERY and s.status is StepStatus.VERIFIED for s in self.steps)

    @property
    def passed(self) -> bool:
        # The composition holds: nothing refused, and the Mission either verified or correctly
        # paused at the consequential boundary (a legitimate stopping point).
        return not self.refused

    def to_projection(self) -> Dict[str, Any]:
        return {
            "goal": self.request.goal, "recipient": self.request.recipient,
            "steps": [s.to_dict() for s in self.steps],
            "pending": self.pending, "pending_reason": self.pending_reason,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "verdict": {"passed": self.passed, "verified": self.verified,
                        "refused": [s.key for s in self.refused]},
        }


def _ok(res: Any) -> bool:
    return bool(getattr(res, "ok", False))


def _oid(res: Any) -> str:
    return str(getattr(res, "provider_object_id", "") or "")


def run_outreach(request: OutreachRequest, *, provider: OutreachProvider,
                 generate_copy: CopyFn, generate_asset: Optional[AssetFn] = None,
                 governance: str = "allow") -> OutreachReport:
    """Run the outreach Mission up to (and possibly through) the activation boundary.

    ``governance`` = "allow" (auto-activate when the provider supports it) or "require_review"
    (pause for a human even when it's automatable). Returns a report that is either verified,
    or ``pending`` at the boundary — call :func:`resume_outreach` after the human acts.
    """
    steps: List[Step] = [Step(PREPARE_OUTREACH, StepStatus.DONE, True,
                              f"target {request.recipient} · {request.company} · {request.campaign_intent}")]

    copy = generate_copy(request)
    if not copy or not getattr(copy, "subject", ""):
        steps.append(Step(GENERATE_COPY, StepStatus.REFUSED, True, "copy generation returned nothing"))
        return OutreachReport(request, tuple(steps))
    steps.append(Step(GENERATE_COPY, StepStatus.DONE, True, f"subject: {copy.subject}"))

    if request.creative_asset and generate_asset is not None:
        asset = generate_asset(request)
        if asset is not None:
            steps.append(Step(GENERATE_ASSET, StepStatus.DONE, True, f"{asset.kind}: {asset.summary or asset.ref}",
                              refs=(asset.ref,)))
        else:
            steps.append(Step(GENERATE_ASSET, StepStatus.SKIPPED, True, "asset generation returned nothing (optional)"))
    else:
        steps.append(Step(GENERATE_ASSET, StepStatus.SKIPPED, True, "no creative asset requested (optional)"))

    cfg = provider.configure_sequence(subject=copy.subject, body_html=copy.body_html)
    if not _ok(cfg):
        steps.append(Step(CONFIGURE_SEQUENCE, StepStatus.REFUSED, True, f"configure failed: {getattr(cfg,'error','')}"))
        return OutreachReport(request, tuple(steps))
    steps.append(Step(CONFIGURE_SEQUENCE, StepStatus.DONE, True, "sequence step + template configured", refs=(_oid(cfg),)))

    en = provider.enroll(recipient=request.recipient)
    if not _ok(en):
        steps.append(Step(ENROLL_RECIPIENT, StepStatus.REFUSED, True, f"enroll failed: {getattr(en,'error','')}"))
        return OutreachReport(request, tuple(steps))
    recipient_ref = _oid(en)
    steps.append(Step(ENROLL_RECIPIENT, StepStatus.DONE, True, f"recipient enrolled ({recipient_ref})", refs=(recipient_ref,)))

    resume = {"recipient": request.recipient, "recipient_ref": recipient_ref, "provider_name": getattr(provider, "provider", "")}
    act = provider.activation()

    # The consequential boundary. Physical first (can we?), then governance (should we un-reviewed?).
    if not act.automatable:
        steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.PENDING_HUMAN, False,
                          f"provider-UI required — {act.detail or act.execution_strategy}"))
        return OutreachReport(request, tuple(steps), pending=True, pending_reason="provider_ui", _resume=resume)
    if governance == "require_review":
        steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.PENDING_HUMAN, True,
                          "governance requires review before activation"))
        return OutreachReport(request, tuple(steps), pending=True, pending_reason="governance", _resume=resume)

    a = provider.activate()
    if not _ok(a):
        steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.REFUSED, True, f"activate failed: {getattr(a,'error','')}"))
        return OutreachReport(request, tuple(steps))
    steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.DONE, True, "activated via API (governance: allow)"))
    return _observe_and_verify(provider, request, recipient_ref, steps, resume)


def resume_outreach(report: OutreachReport, *, provider: OutreachProvider,
                    approved: bool = True) -> OutreachReport:
    """Resume after the human acted at the boundary: for a provider-UI pause the human toggled
    it on (we just observe/verify); for a governance pause an approval lets us activate now."""
    if not report.pending or report._resume is None:
        return report
    resume = report._resume
    recipient_ref = resume["recipient_ref"]

    # Already past the boundary — just re-observe until delivery is confirmed.
    if report.pending_reason == "awaiting_delivery":
        kept = [s for s in report.steps if s.key not in (OBSERVE_SEND, VERIFY_DELIVERY)]
        return _observe_and_verify(provider, report.request, recipient_ref, kept, resume)

    steps: List[Step] = [s for s in report.steps if s.key != ACTIVATE_SEQUENCE]

    if report.pending_reason == "governance":
        if not approved:
            steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.REJECTED, True, "a human declined — nothing sent"))
            return OutreachReport(report.request, tuple(steps))
        a = provider.activate()
        if not _ok(a):
            steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.REFUSED, True, f"activate failed: {getattr(a,'error','')}"))
            return OutreachReport(report.request, tuple(steps))
        steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.DONE, True, "activated after approval"))
    else:  # provider_ui — the human toggled it on in the provider
        steps.append(Step(ACTIVATE_SEQUENCE, StepStatus.DONE, False, "activated by a human in the provider UI"))

    return _observe_and_verify(provider, report.request, recipient_ref, steps, resume)


def _observe_and_verify(provider: OutreachProvider, request: OutreachRequest, recipient_ref: str,
                        steps: List[Step], resume: Dict[str, Any]) -> OutreachReport:
    obs = provider.observe(recipient=request.recipient)
    status = str(getattr(obs, "status", "") or "")
    steps.append(Step(OBSERVE_SEND, StepStatus.DONE, True, f"status={status or 'unknown'}"))

    delivered = bool(getattr(obs, "delivered", False)) or status in ("sent", "delivered")
    queued = status in ("active", "scheduled", "pending")
    if getattr(obs, "bounced", False) or status in ("bounced", "failed"):
        steps.append(Step(VERIFY_DELIVERY, StepStatus.REFUSED, True, f"delivery failed: {status}"))
        return OutreachReport(request, tuple(steps), _resume=resume)
    if delivered:
        receipt = ExecutionReceipt(provider=resume.get("provider_name", "") or "outreach", object_id=recipient_ref,
                                   status=status or "delivered", observed_at=str(getattr(obs, "observed_at", "") or ""))
        steps.append(Step(VERIFY_DELIVERY, StepStatus.VERIFIED, True, f"delivered · {status}", refs=(recipient_ref,)))
        return OutreachReport(request, tuple(steps), receipt=receipt)
    # queued but not yet delivered — verification stays open; resume again later to confirm
    steps.append(Step(VERIFY_DELIVERY, StepStatus.OBSERVING, True,
                      f"queued ({status or 'unknown'}) — not yet delivered; observe again to verify"))
    return OutreachReport(request, tuple(steps), pending=True, pending_reason="awaiting_delivery", _resume=resume)
