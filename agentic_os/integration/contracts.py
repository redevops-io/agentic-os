"""Integration plane — core data contracts (Phase 0).

The durable objects that let ReDevOps carry a business obligation across several apps and prove the final state,
rather than fire-and-forget API calls. The central primitive is the :class:`Obligation`: a statement of what
downstream business state MUST become true after an upstream event — satisfied only when that state is
*independently observed*, never because a POST returned 200.

These contracts are open (community): correctness is not proprietary. The SaaS connector fleet, the Control
Tower, and multi-tenant governance live in the enterprise overlay. See
~/Documents/REDEVOPS_THIRD_PARTY_INTEGRATION_FAILURES_AUDIT_IMPLEMENTATION_PLAN_2026-10-04.md (§3).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from runtime_contracts.protocol import content_hash


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── enums ────────────────────────────────────────────────────────────────────────────────────────────────
class ObligationStatus(str, Enum):
    PENDING = "pending"
    SATISFIED = "satisfied"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    WAIVED = "waived"


class EntityType(str, Enum):
    PERSON = "person"
    ORGANIZATION = "organization"
    ACCOUNT = "account"
    CONTRACT = "contract"
    INVOICE = "invoice"
    PAYMENT = "payment"
    TICKET = "ticket"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    CONFLICTED = "conflicted"


class ExceptionCategory(str, Enum):
    OBLIGATION_UNSATISFIED = "obligation_unsatisfied"   # expected state never observed (silent failure)
    OBLIGATION_CONFLICT = "obligation_conflict"         # destination observed with a conflicting value
    OBLIGATION_OVERDUE = "obligation_overdue"           # deadline exceeded
    ACTION_FAILED = "action_failed"                     # the write itself errored
    IDENTITY_CONFLICT = "identity_conflict"
    RECONCILIATION_VARIANCE = "reconciliation_variance"
    SYNC_CONFLICT = "sync_conflict"                     # systems disagree on a field; no safe convergent value


# ── resource + evidence ──────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Resource:
    """A connected application / account / object namespace (§3.1)."""
    provider: str
    account_or_workspace_id: str = ""
    tenant_id: str = ""
    endpoint: str = ""
    capabilities: tuple[str, ...] = ()
    credential_ref: str = ""
    authority_policy: str = ""
    resource_id: str = field(default_factory=lambda: _new_id("res"))


@dataclass(frozen=True)
class Observation:
    """Immutable evidence read from an external system (§3.2). ``observed_for`` (the period the value describes)
    is kept distinct from ``fetched_at`` (when it became available) — never conflate them."""
    resource_id: str
    object_type: str
    external_id: str
    normalized_fields: dict[str, Any] = field(default_factory=dict)
    observed_for: str = ""                 # the business period/instant the value describes
    known_at: str = ""                     # when the value became knowable
    fetched_at: str = field(default_factory=_now)
    source_version: str = ""
    evidence_ref: str = ""
    observation_id: str = field(default_factory=lambda: _new_id("obs"))

    @property
    def payload_digest(self) -> str:
        return content_hash(self.normalized_fields)


@dataclass(frozen=True)
class CanonicalEntity:
    """A resolved cross-system identity (§3.3). Probabilistic matches are never silently merged."""
    entity_type: EntityType
    aliases: tuple[str, ...] = ()
    source_bindings: tuple[tuple[str, str], ...] = ()   # (resource_id, external_id) pairs
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    entity_id: str = field(default_factory=lambda: _new_id("ent"))


# ── the central primitive ────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_s: float = 0.0                 # Phase-0 engine is synchronous; backoff is advisory


@dataclass(frozen=True)
class Obligation:
    """What downstream business state MUST become true after an upstream event (§3.4). Frozen; the engine
    produces new revisions via ``dataclasses.replace`` — append-only, like a sealed plan.

    ``expected_state`` maps ``object_type`` → ``{field: expected_value}`` the destination must show on read-back.
    """
    trigger: str                                   # e.g. "salesforce.opportunity.closed_won"
    expected_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_resource: str = ""
    destination_resource: str = ""
    entity_refs: tuple[str, ...] = ()
    workflow_id: str = ""
    deadline: str = ""                             # ISO; "" = none
    status: ObligationStatus = ObligationStatus.PENDING
    verification_policy: str = "read_after_write"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    escalation_policy: str = "exception"
    attempts: int = 0
    obligation_id: str = field(default_factory=lambda: _new_id("obl"))
    created_at: str = field(default_factory=_now)


# ── reconciliation + sync (contracts only in Phase 0) ────────────────────────────────────────────────────
@dataclass(frozen=True)
class ReconciliationItem:
    left_evidence: str = ""
    right_evidence: str = ""
    canonical_amount: float = 0.0
    dimensions: tuple[tuple[str, str], ...] = ()
    match_type: str = ""                   # exact | tolerance | probabilistic | unmatched
    variance: float = 0.0
    status: str = "open"
    explanation: str = ""
    item_id: str = field(default_factory=lambda: _new_id("rec"))


@dataclass(frozen=True)
class SyncState:
    canonical_entity: str
    field_or_state: str
    sources: tuple[str, ...] = ()
    authority_order: tuple[str, ...] = ()
    last_agreed_value: Any = None
    conflicting_values: tuple[tuple[str, Any], ...] = ()
    conflict_policy: str = "authority_order"   # authority_order | most_restrictive | latest
    resolution: Any = None


# ── exceptions + receipts ────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IntegrationException:
    """A failure as a durable domain object (§3.7), not a log line — rankable, replayable."""
    category: ExceptionCategory
    obligation_id: str = ""
    workflow_id: str = ""
    affected_entities: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    detail: str = ""
    business_impact: str = ""
    recommended_action: str = ""
    retry_count: int = 0
    first_seen: str = field(default_factory=_now)
    last_attempt: str = field(default_factory=_now)
    status: str = "open"
    exception_id: str = field(default_factory=lambda: _new_id("exc"))


@dataclass(frozen=True)
class IntegrationReceipt:
    """Links trigger → evidence → authority → action → OBSERVED destination state (§3.8). Proof the business
    outcome actually happened, not that an API returned 200."""
    obligation_id: str
    trigger: str
    action: str
    satisfied: bool
    observed_state: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    authority: str = ""
    ts: str = field(default_factory=_now)
    receipt_id: str = field(default_factory=lambda: _new_id("rcpt"))

    def digest(self) -> str:
        return content_hash({"obligation": self.obligation_id, "trigger": self.trigger, "action": self.action,
                             "satisfied": self.satisfied, "observed": self.observed_state})
