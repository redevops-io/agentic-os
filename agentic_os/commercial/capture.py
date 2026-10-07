"""Unified Capture Gateway — capture commercial state where work happens (plan §21/§44-§48, §5-§6).

The strongest design requirement in the CRM audit: *the salesperson should not have to maintain the CRM for the
CRM to stay useful.* Every channel (email, calendar, calls, Chatwoot, quotes, ERP, billing, Sidekick chat, Slack,
…) normalizes through ONE ``CaptureEvent`` → entity resolution → ``CommercialActivity`` → ``CRMProjection``, and
proposed CRM mutations are classified **review-by-exception**: harmless inferences auto-apply, judgement/consequence
is gated. Channel-specific integrations must NOT fork their own business logic — they only produce ``CaptureEvent``.

This module is the provider-neutral spine (contracts + the deterministic classifier + a reference gateway). The
enterprise overlay binds real providers and the entity resolver (``integration.identity.resolve``) and persists
evidence; nothing here does I/O.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

from ..sidekick.contracts import SurfaceContext


def _now_ms() -> int:
    return int(time.time() * 1000)


class CaptureChannel(str, Enum):
    EMAIL = "email"; CALENDAR = "calendar"; CALL = "call"; MEETING = "meeting"; VOICE_NOTE = "voice_note"
    SIDEKICK_CHAT = "sidekick_chat"; SLACK = "slack"; TEAMS = "teams"; WHATSAPP = "whatsapp"
    CHATWOOT = "chatwoot"; CRM = "crm"; ERP = "erp"; BILLING = "billing"; CONTRACT = "contract"
    QUOTE = "quote"; PRODUCT_USAGE = "product_usage"; WEB_ACTIVITY = "web_activity"; MANUAL = "manual"


class ProjectionPolicy(str, Enum):
    """How a proposed CRM mutation is handled (plan §6). Attention is spent only where judgement matters."""
    AUTO_SAFE = "auto_safe"                     # apply without asking
    REVIEW_RECOMMENDED = "review_recommended"   # surface in the capture inbox, not an interruption
    APPROVAL_REQUIRED = "approval_required"     # consequential → explicit approval before side effect
    MANUAL_ONLY = "manual_only"                 # high-judgement / legally sensitive → human does it


# change_kind → policy (plan §6). Unknown kinds default to REVIEW_RECOMMENDED (surface, don't block, don't auto-apply).
_AUTO_SAFE = {"log_activity", "update_last_contacted", "link_conversation", "refresh_usage", "attach_meeting"}
_REVIEW = {"infer_next_follow_up", "add_meeting_summary", "infer_stakeholder_role", "suggest_blocker",
           "suggest_stage_transition"}
_APPROVAL = {"send_customer_email", "change_offer", "apply_discount", "commit_delivery_date", "send_quote"}
_MANUAL = {"contract_change", "write_off", "exceptional_pricing"}


def classify_change(change_kind: str) -> ProjectionPolicy:
    k = (change_kind or "").strip().lower()
    if k in _AUTO_SAFE:
        return ProjectionPolicy.AUTO_SAFE
    if k in _APPROVAL:
        return ProjectionPolicy.APPROVAL_REQUIRED
    if k in _MANUAL:
        return ProjectionPolicy.MANUAL_ONLY
    return ProjectionPolicy.REVIEW_RECOMMENDED


@dataclass(frozen=True)
class CaptureEvent:
    """A normalized unit of captured work, from any channel (§47). Raw content is referenced, not inlined."""
    event_id: str
    channel: CaptureChannel
    tenant_id: str = ""
    principal: str = ""
    source_resource: str = ""
    occurred_at: str = ""
    known_at: str = ""
    participants: Tuple[str, ...] = ()
    raw_content_ref: str = ""
    attachments: Tuple[str, ...] = ()
    candidate_entities: Tuple[str, ...] = ()
    surface_context: Optional[SurfaceContext] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommercialActivity:
    """A resolved commercial interaction (§29). ``occurred_at`` (when it happened) is distinct from ``known_at``."""
    activity_id: str
    channel: CaptureChannel
    tenant_id: str = ""
    customer: str = ""
    opportunity: str = ""
    people: Tuple[str, ...] = ()
    occurred_at: str = ""
    known_at: str = ""
    source: str = ""
    summary: str = ""
    evidence: Tuple[str, ...] = ()
    commitments: Tuple[str, ...] = ()
    objections: Tuple[str, ...] = ()
    next_steps: Tuple[str, ...] = ()
    confidence: float = 1.0


@dataclass(frozen=True)
class ChangeProposal:
    """A single proposed CRM mutation with its classified handling policy (§30)."""
    record: str                                  # e.g. "opportunity:acme"
    field: str
    value: Any
    change_kind: str
    evidence: Tuple[str, ...] = ()
    policy: ProjectionPolicy = ProjectionPolicy.REVIEW_RECOMMENDED

    @property
    def requires_review(self) -> bool:
        return self.policy is not ProjectionPolicy.AUTO_SAFE


@dataclass(frozen=True)
class CRMProjection:
    """The CRM is a PROJECTION of commercial reality, not the place it is re-entered (§30). Raw evidence stays
    immutable; this projection can be corrected and rebuilt."""
    activity_id: str
    record: str
    proposed_changes: Tuple[ChangeProposal, ...] = ()
    evidence: Tuple[str, ...] = ()
    confidence: float = 1.0

    @property
    def auto_safe(self) -> Tuple[ChangeProposal, ...]:
        return tuple(c for c in self.proposed_changes if c.policy is ProjectionPolicy.AUTO_SAFE)

    @property
    def needs_review(self) -> Tuple[ChangeProposal, ...]:
        return tuple(c for c in self.proposed_changes if c.requires_review)


@dataclass(frozen=True)
class CaptureExtraction:
    """What a channel-neutral extractor produces from a CaptureEvent: the activity + its raw proposed changes
    (policy left unset — the gateway classifies it)."""
    activity: CommercialActivity
    changes: Tuple[ChangeProposal, ...] = ()
    record: str = ""


class CommercialCaptureProvider(Protocol):
    """A channel adapter. Its ONLY job is to emit normalized CaptureEvents — no CRM business logic (§48)."""
    channel: CaptureChannel
    def poll(self) -> Sequence[CaptureEvent]: ...
    def acknowledge(self, event_id: str) -> None: ...


# An extractor turns a CaptureEvent into a resolved activity + proposed changes. The enterprise overlay supplies
# one backed by entity resolution + an LLM; a deterministic default is used for testing/degraded mode.
Extractor = Callable[[CaptureEvent], CaptureExtraction]


@dataclass
class CaptureGateway:
    """Reference capture pipeline: register channel providers, ingest CaptureEvents through one extractor, and emit
    a policy-classified CRMProjection. Deterministic; the classifier is the review-by-exception core."""
    extractor: Extractor
    _providers: list = field(default_factory=list)

    def register(self, provider: CommercialCaptureProvider) -> None:
        self._providers.append(provider)

    def classify(self, extraction: CaptureExtraction) -> CRMProjection:
        record = extraction.record or (extraction.changes[0].record if extraction.changes else "")
        classified = tuple(replace(c, policy=classify_change(c.change_kind)) for c in extraction.changes)
        evidence = tuple(sorted({e for c in classified for e in c.evidence} | set(extraction.activity.evidence)))
        return CRMProjection(activity_id=extraction.activity.activity_id, record=record,
                             proposed_changes=classified, evidence=evidence,
                             confidence=extraction.activity.confidence)

    def ingest(self, event: CaptureEvent) -> Tuple[CommercialActivity, CRMProjection]:
        extraction = self.extractor(event)
        return extraction.activity, self.classify(extraction)

    def poll_all(self) -> Tuple[Tuple[CommercialActivity, CRMProjection], ...]:
        out = []
        for p in self._providers:
            for ev in p.poll():
                out.append(self.ingest(ev))
                p.acknowledge(ev.event_id)
        return tuple(out)


__all__ = [
    "CaptureChannel", "ProjectionPolicy", "CaptureEvent", "CommercialActivity", "ChangeProposal",
    "CRMProjection", "CaptureExtraction", "CommercialCaptureProvider", "Extractor", "CaptureGateway",
    "classify_change",
]
