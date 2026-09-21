"""S1 — social contracts (plan §23, §27, §31).

Normalized, evidence-preserving, provider-aware. Two rules are load-bearing:
  * **UNKNOWN is a first-class value** (plan §24): a signal is PRESENT / ABSENT / UNKNOWN — never
    defaulted to a convenient answer. Someone complaining is not automatically a lead.
  * **Provenance is never erased** (plan §23): an observation keeps its provider, retrieval method and
    provider-policy reference, so downstream use stays within what the provider's terms permit.
"""
from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

SOCIAL_CONTRACT_VERSION = "agent-gateway/v1"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class Signal(str, enum.Enum):
    """A tri-state evidence signal. UNKNOWN is preserved, never collapsed to ABSENT/PRESENT."""
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class ActionClass(str, enum.Enum):
    """Governed social action classes (plan §28). Publishing to an owned channel is distinct from
    contacting an individual; each may carry a different Governance policy."""
    PUBLISH_OWNED_CHANNEL = "social.publish_owned_channel"
    REPLY_PUBLIC = "social.reply_public"
    REPLY_TO_EXISTING_THREAD = "social.reply_to_existing_thread"
    CONTACT_INDIVIDUAL = "social.contact_individual"
    SEND_DM = "social.send_dm"


# ── observation (plan §23) ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SocialObservation:
    """One piece of PERMITTED public social content, provenance intact."""
    provider: str
    source_type: str                # post | comment | thread | ...
    source_ref: str
    text: str = ""
    author_ref: str = ""
    thread_ref: str = ""
    observed_at: int = field(default_factory=_now_ms)
    published_at: int = 0
    public_visibility: bool = True
    retrieval_method: str = ""       # e.g. "reddit-api", "fixture" — never "scrape" for a production path
    provider_policy_ref: str = ""    # the policy/version identity the retrieval complied with
    evidence_ref: str = ""
    provider_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_ref:
            object.__setattr__(self, "evidence_ref", "soev:" + _digest(
                {"p": self.provider, "s": self.source_ref, "t": self.text})[7:23])

    def freshness_seconds(self, *, now_ms: Optional[int] = None) -> Optional[float]:
        if not self.published_at:
            return None
        return ((now_ms or _now_ms()) - self.published_at) / 1000.0


# ── signals (plan §24, §31) ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProblemSignal:
    present: Signal
    description: str = ""
    evidence_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentSignal:
    """solution_seeking and commercial_intent are SEPARATE (plan §31) — a request for a solution is not
    a signal of readiness to buy. Each is tri-state so UNKNOWN survives."""
    solution_seeking: Signal = Signal.UNKNOWN
    commercial_intent: Signal = Signal.UNKNOWN
    evidence_refs: Tuple[str, ...] = ()


# ── opportunity + market signal (plan §24, §26) ──────────────────────────────────────
@dataclass(frozen=True)
class SocialOpportunity:
    opportunity_id: str
    provider: str
    source_ref: str
    subject_ref: str
    problem: ProblemSignal
    intent: IntentSignal
    product_fit: Signal = Signal.UNKNOWN
    freshness_seconds: Optional[float] = None
    evidence_sufficiency: Signal = Signal.UNKNOWN
    component_scores: Mapping[str, float] = field(default_factory=dict)   # explainable dimensions
    confidence: float = 0.0
    proposed_actions: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()

    @staticmethod
    def new(**kw) -> "SocialOpportunity":
        return SocialOpportunity(opportunity_id="sopp_" + uuid.uuid4().hex[:12], **kw)

    def explain(self) -> dict:
        """Why surfaced, which evidence, which dimensions are unknown (plan §25)."""
        unknowns = [k for k, v in {"solution_seeking": self.intent.solution_seeking,
                                   "commercial_intent": self.intent.commercial_intent,
                                   "product_fit": self.product_fit,
                                   "evidence_sufficiency": self.evidence_sufficiency}.items()
                    if v is Signal.UNKNOWN]
        return {"opportunity_id": self.opportunity_id, "problem": self.problem.description,
                "provider": self.provider, "freshness_seconds": self.freshness_seconds,
                "components": dict(self.component_scores), "confidence": self.confidence,
                "unknown_dimensions": unknowns, "evidence_refs": list(self.evidence_refs),
                "proposed_actions": list(self.proposed_actions)}


@dataclass(frozen=True)
class MarketSignal:
    topic: str
    window_seconds: int
    observation_count: int
    unique_thread_count: int
    source_mix: Mapping[str, int] = field(default_factory=dict)
    trend: str = "flat"                 # rising | falling | flat
    representative_evidence_refs: Tuple[str, ...] = ()
    competing_explanations: Tuple[str, ...] = ()
    confidence: float = 0.0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["source_mix"] = dict(self.source_mix)
        d["representative_evidence_refs"] = list(self.representative_evidence_refs)
        d["competing_explanations"] = list(self.competing_explanations)
        return d


# ── content + engagement (plan §27, §28) ─────────────────────────────────────────────
@dataclass(frozen=True)
class ContentDraft:
    content: str
    target_platform: str
    evidence_refs: Tuple[str, ...] = ()
    draft_id: str = field(default_factory=lambda: "draft_" + uuid.uuid4().hex[:12])

    @property
    def content_digest(self) -> str:
        """Content-address the exact draft. An edit changes this, invalidating a prior approval (§27)."""
        return _digest({"content": self.content, "platform": self.target_platform})


@dataclass(frozen=True)
class EngagementProposal:
    opportunity_ref: str
    action_class: ActionClass
    draft: ContentDraft
    target: str = ""
    rationale: str = ""
    proposal_id: str = field(default_factory=lambda: "engp_" + uuid.uuid4().hex[:12])


# ── governed action request + receipt (plan §27, §28) ────────────────────────────────
@dataclass(frozen=True)
class SocialActionRequest:
    """Binds an EXACT content digest + target + action class. Governance authorizes this; a changed
    draft yields a different digest and thus a different request (fail-closed on mutation)."""
    action_class: ActionClass
    provider: str
    content_digest: str
    target: str
    account: str = ""
    evidence_refs: Tuple[str, ...] = ()
    request_id: str = field(default_factory=lambda: "sar_" + uuid.uuid4().hex[:12])
    created_at: int = field(default_factory=_now_ms)

    def intent_digest(self) -> str:
        return _digest({"action": self.action_class.value, "provider": self.provider,
                        "content_digest": self.content_digest, "target": self.target,
                        "account": self.account})


@dataclass(frozen=True)
class SocialActionReceipt:
    """Proof a governed social action occurred (plan §27). ``verification_state`` separates the
    provider's response from ReDevOps's observed verification."""
    provider: str
    account: str
    action_class: str
    content_digest: str
    target_surface: str
    status: str                     # SUCCEEDED | FAILED | HELD
    provider_post_id: str = ""
    provider_post_url: str = ""
    published_at: int = 0
    provider_response: str = ""
    verification_state: str = "unverified"
    decision_id: str = ""
    error: str = ""
    receipt_id: str = field(default_factory=lambda: "srcpt_" + uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        return self.__dict__.copy()
