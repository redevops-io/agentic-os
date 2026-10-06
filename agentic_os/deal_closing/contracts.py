"""Deal Closing Intelligence — core contracts (Phase 1, plan §6-§8).

Closing is treated as a governed cross-system DECISION problem, not a methodology coach. The first-class product
distinction this module encodes is **CRM-REPORTED state vs RUNTIME-VERIFIED state**: a CRM can assert "Negotiation,
80 % close this month", but that assertion is only *reported* until the runtime independently reads it back from the
system that actually holds the fact. Reported and verified values are kept separate and are NEVER silently
reconciled — a disagreement surfaces as CONFLICTED, exactly as the entity-resolution plane refuses to merge on
conflict (:class:`agentic_os.integration.identity`).

Everything here is a :class:`~agentic_os.integrations.business.contracts.BusinessObject`: content-addressed,
provenanced, bitemporal, money in integer minor units. No methodology logic lives here (that is Phase 2); this is
the state substrate those later phases reason over.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Tuple

from ..integrations.business.contracts import BusinessObject, Provenance, now_ms


# ── reported-vs-verified claim ────────────────────────────────────────────────────────────────────────
class ClaimStatus(str, Enum):
    """The relationship between what a source REPORTED and what the runtime independently VERIFIED."""
    UNVERIFIED = "unverified"      # a source asserted it; nothing has read it back yet
    VERIFIED = "verified"          # an independent read-back confirmed the reported value
    CONFLICTED = "conflicted"      # reported and verified disagree — never auto-reconciled
    STALE = "stale"                # it was verified, but the confirming evidence is now too old to trust


@dataclass(frozen=True)
class Claim:
    """A single fact about a deal, separating the value a system REPORTED from the value independently VERIFIED.

    ``reported`` is what a system (usually the CRM) asserts; ``verified`` is the value a *different*, independent
    read confirmed (``None`` until verification runs). The two are compared, never merged: if both are present and
    differ, the claim is CONFLICTED. ``known_at`` is the fact-time of the verifying evidence (for freshness)."""
    field_name: str
    reported: Any = None
    reported_source: str = ""              # system that asserted ``reported`` (e.g. "salesforce")
    verified: Any = None                   # None = not independently verified yet
    verified_source: str = ""              # the independent source that confirmed ``verified``
    evidence_refs: Tuple[str, ...] = ()
    verified_at: int = 0                   # when the verification was observed (ms); 0 = never
    known_at: int = 0                      # fact time of the verifying evidence (ms); 0 = unknown
    confidence: float = 0.0

    def status(self, *, now: int | None = None, max_age_ms: int | None = None) -> ClaimStatus:
        """Honest status. Unverified until ``verified`` is set; CONFLICTED if reported≠verified; STALE if the
        verifying evidence is older than ``max_age_ms``; otherwise VERIFIED."""
        if self.verified is None:
            return ClaimStatus.UNVERIFIED
        if self.reported is not None and self.reported != self.verified:
            return ClaimStatus.CONFLICTED
        if max_age_ms is not None and self.verified_at:
            ref = now if now is not None else now_ms()
            if ref - self.verified_at > max_age_ms:
                return ClaimStatus.STALE
        return ClaimStatus.VERIFIED


# ── the deal ──────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Deal(BusinessObject):
    """A persistent opportunity to close, holding the CRM-REPORTED snapshot alongside the runtime-VERIFIED
    overlay. The ``reported_*`` fields are what the source systems assert; ``claims`` is the independent
    verification record for the fields that matter. ``source_systems`` names every system a fact was drawn from."""
    KIND: ClassVar[str] = "deal_closing.deal"
    tenant: str = ""
    account_ref: str = ""
    opportunity_ref: str = ""                  # the CRM opportunity id (verification re-reads this)
    owner_ref: str = ""
    # CRM-REPORTED snapshot
    reported_stage: str = ""
    reported_amount_cents: int = 0
    currency: str = ""
    reported_close_date: str = ""              # ISO date the CRM expects to close
    reported_probability: float = 0.0          # the CRM's own close probability (0..1)
    products: Tuple[str, ...] = ()
    source_systems: Tuple[str, ...] = ()
    # RUNTIME-VERIFIED overlay — independent read-back on the fields that matter
    claims: Tuple[Claim, ...] = ()

    def claim(self, field_name: str) -> Claim | None:
        for c in self.claims:
            if c.field_name == field_name:
                return c
        return None

    def unverified_fields(self, *, now: int | None = None, max_age_ms: int | None = None) -> Tuple[str, ...]:
        """Fields with a claim that is not currently VERIFIED (unverified / conflicted / stale)."""
        return tuple(c.field_name for c in self.claims
                     if c.status(now=now, max_age_ms=max_age_ms) is not ClaimStatus.VERIFIED)

    def conflicts(self) -> Tuple[Claim, ...]:
        return tuple(c for c in self.claims if c.status() is ClaimStatus.CONFLICTED)

    def reported_probability_supported(self, *, now: int | None = None, max_age_ms: int | None = None) -> bool:
        """The CRM's close probability is 'supported' only when none of its verified fields contradict it and
        nothing material is left unverified. This is deliberately conservative: absence of verification is NOT
        support. A caller with an empty claim set gets ``False`` whenever the CRM is already claiming a high
        probability it has shown no evidence for."""
        if self.conflicts():
            return False
        if self.reported_probability >= 0.5 and not self.claims:
            return False
        return len(self.unverified_fields(now=now, max_age_ms=max_age_ms)) == 0


# ── evidence ──────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DealEvidence(BusinessObject):
    """A single piece of evidence attached to a deal claim or condition. Thin wrapper over provenance — the raw
    payload stays an evidence artifact referenced by ``prov.evidence_refs``, never inlined."""
    KIND: ClassVar[str] = "deal_closing.evidence"
    deal_ref: str = ""
    kind_of: str = ""                          # e.g. "crm.activity" | "email.thread" | "quote" | "security.review"
    summary: str = ""
    supports: str = ""                         # the claim field or condition name this bears on
    confidence: float = 0.0


# ── buying committee ──────────────────────────────────────────────────────────────────────────────────
class CommitteeRole(str, Enum):
    ECONOMIC_BUYER = "economic_buyer"
    CHAMPION = "champion"
    TECHNICAL_BUYER = "technical_buyer"
    PROCUREMENT = "procurement"
    LEGAL = "legal"
    SECURITY = "security"
    FINANCE = "finance"
    EXEC_SPONSOR = "exec_sponsor"
    USER = "user"
    DETRACTOR = "detractor"
    UNKNOWN = "unknown"


class RoleStatus(str, Enum):
    """Evidence-backed confidence that a person actually holds a role — mirrors
    :class:`agentic_os.integration.contracts.ResolutionStatus`: a conflict surfaces, it is never merged away."""
    UNVERIFIED = "unverified"      # inferred from a title/signal, not confirmed
    PROBABLE = "probable"          # supporting evidence, below the confirm bar
    CONFLICTED = "conflicted"      # evidence disagrees (e.g. two people claimed economic buyer)
    VERIFIED = "verified"          # confirmed by direct evidence (named themselves / signs / holds budget)


@dataclass(frozen=True)
class CommitteeMember:
    """One stakeholder and the evidence-backed claim that they hold a role. ``entity_ref`` is the canonical
    entity id from the resolution plane, so the same person across CRM/email/support is one member."""
    entity_ref: str
    name: str = ""
    role: CommitteeRole = CommitteeRole.UNKNOWN
    role_status: RoleStatus = RoleStatus.UNVERIFIED
    confidence: float = 0.0
    evidence_refs: Tuple[str, ...] = ()
    title: str = ""                            # the raw title/signal the inference started from


@dataclass(frozen=True)
class BuyingCommittee(BusinessObject):
    """The set of stakeholders on a deal with evidence-backed roles. Multiple people may claim the same role;
    the committee does not pick a winner — it exposes that as a conflict for the blocker engine (Phase 3)."""
    KIND: ClassVar[str] = "deal_closing.buying_committee"
    deal_ref: str = ""
    members: Tuple[CommitteeMember, ...] = ()

    def by_role(self, role: CommitteeRole) -> Tuple[CommitteeMember, ...]:
        return tuple(m for m in self.members if m.role is role)

    def role_status(self, role: CommitteeRole) -> RoleStatus:
        """Aggregate status for a role across its claimants. No claimant → UNVERIFIED; any two VERIFIED/PROBABLE
        claimants for an exclusive role → CONFLICTED; one VERIFIED → VERIFIED; else the best single status."""
        members = self.by_role(role)
        if not members:
            return RoleStatus.UNVERIFIED
        asserted = [m for m in members if m.role_status in (RoleStatus.VERIFIED, RoleStatus.PROBABLE)]
        if role in _EXCLUSIVE_ROLES and len(asserted) > 1:
            return RoleStatus.CONFLICTED
        if any(m.role_status is RoleStatus.CONFLICTED for m in members):
            return RoleStatus.CONFLICTED
        if any(m.role_status is RoleStatus.VERIFIED for m in members):
            return RoleStatus.VERIFIED
        if any(m.role_status is RoleStatus.PROBABLE for m in members):
            return RoleStatus.PROBABLE
        return RoleStatus.UNVERIFIED

    def missing_roles(self, required: Tuple[CommitteeRole, ...]) -> Tuple[CommitteeRole, ...]:
        """Required roles that are not currently VERIFIED (so UNVERIFIED/PROBABLE/CONFLICTED all count missing —
        a role is only 'covered' once it is confirmed)."""
        return tuple(r for r in required if self.role_status(r) is not RoleStatus.VERIFIED)


_EXCLUSIVE_ROLES = frozenset(
    {CommitteeRole.ECONOMIC_BUYER, CommitteeRole.EXEC_SPONSOR}
)


__all__ = [
    "Claim", "ClaimStatus",
    "Deal", "DealEvidence",
    "CommitteeRole", "RoleStatus", "CommitteeMember", "BuyingCommittee",
    "Provenance",
]
