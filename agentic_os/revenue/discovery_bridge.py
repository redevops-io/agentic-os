"""Discovery → Revenue Mission bridge (consumes revenue-handoff/v1).

The boring, contract-driven seam that turns a qualified Discovery opportunity (from the geographic
qualification in context-runtime's `local_gov`, or any collector that emits the same contract) into a
governed Revenue Mission — preserving the qualification evidence, source provenance, geographic
reasoning, deadlines and confidence, and carrying evidence IDENTITY (references), never a copy.

Idempotency is on the Discovery opportunity identity (`opportunity_id`): a rediscovered, amended, or
re-qualified tender UPDATES/reawakens the existing mission rather than creating another lead — the
thing that matters the moment you point this at real municipal portals.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .contracts import OpportunityType, Priority, RevenueOpportunity
from .gateway import AttentionChannel
from .runner import RevenueMissionRun

HANDOFF_CONTRACT_VERSION = "revenue-handoff/v1"


@dataclass
class Qualification:
    decision: str                       # PURSUE | REVIEW | REJECT
    reasons: tuple[str, ...] = ()
    service_match: float = 0.0
    distance_miles: Optional[float] = None


@dataclass
class QualifiedOpportunity:
    """The revenue-handoff/v1 record as the Mission side sees it (mirror of local_gov.to_handoff)."""
    opportunity_id: str
    source: str
    issuing_entity: str
    jurisdiction_id: str
    title: str
    summary: str
    solicitation_type: str
    qualification: Qualification
    place_of_performance_zip: str = ""
    geographic_distance_miles: Optional[float] = None
    categories: tuple[str, ...] = ()
    posted_at: str = ""
    response_due_at: str = ""
    estimated_value: Optional[float] = None
    required_licenses: tuple[str, ...] = ()
    required_certifications: tuple[str, ...] = ()
    source_url: str = ""
    evidence_ids: tuple[str, ...] = ()       # references to Discovery evidence
    discovered_at: str = ""
    geographic_reasoning: tuple[str, ...] = ()
    confidence: Optional[float] = None
    contract_version: str = HANDOFF_CONTRACT_VERSION

    @classmethod
    def from_dict(cls, d: dict) -> "QualifiedOpportunity":
        q = d.get("qualification") or {}
        ver = d.get("contract_version", "")
        if ver and ver != HANDOFF_CONTRACT_VERSION:
            raise ValueError(f"unsupported handoff contract {ver!r}; expected {HANDOFF_CONTRACT_VERSION}")
        return cls(
            opportunity_id=d["opportunity_id"], source=d.get("source", ""),
            issuing_entity=d.get("issuing_entity", ""), jurisdiction_id=d.get("jurisdiction_id", ""),
            title=d.get("title", ""), summary=d.get("summary", "") or d.get("title", ""),
            solicitation_type=d.get("solicitation_type", ""),
            qualification=Qualification(
                decision=q.get("decision", "REVIEW"), reasons=tuple(q.get("reasons", ())),
                service_match=q.get("service_match", 0.0), distance_miles=q.get("distance_miles")),
            place_of_performance_zip=d.get("place_of_performance_zip", ""),
            geographic_distance_miles=d.get("geographic_distance_miles"),
            categories=tuple(d.get("categories", ())), posted_at=d.get("posted_at", ""),
            response_due_at=d.get("response_due_at", ""), estimated_value=d.get("estimated_value"),
            required_licenses=tuple(d.get("required_licenses", ())),
            required_certifications=tuple(d.get("required_certifications", ())),
            source_url=d.get("source_url", ""), evidence_ids=tuple(d.get("evidence_ids", ())),
            discovered_at=d.get("discovered_at", ""),
            geographic_reasoning=tuple(d.get("geographic_reasoning", ())),
            confidence=d.get("confidence"), contract_version=ver or HANDOFF_CONTRACT_VERSION)


def should_open(qo: QualifiedOpportunity) -> bool:
    """Only PURSUE — and urgent REVIEW (a near deadline) — become owner-facing missions. Plain REVIEW
    and REJECT stay in the discovery layer (the plan's '3 to review, not 137 bids')."""
    if qo.qualification.decision == "PURSUE":
        return True
    return qo.qualification.decision == "REVIEW" and bool(qo.response_due_at)


def opportunity_digest(qo: QualifiedOpportunity) -> str:
    """Material-content hash for idempotency. Changes when the *substance* changes (deadline, value,
    requirements, qualification decision, summary) — NOT on re-observation timestamps or confidence
    jitter, so a plain rediscovery is a no-op while a real amendment reawakens the mission."""
    material = {
        "summary": qo.summary, "solicitation_type": qo.solicitation_type,
        "response_due_at": qo.response_due_at, "estimated_value": qo.estimated_value,
        "required_licenses": sorted(qo.required_licenses),
        "required_certifications": sorted(qo.required_certifications),
        "decision": qo.qualification.decision, "source_url": qo.source_url,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _priority(qo: QualifiedOpportunity) -> Priority:
    if qo.response_due_at:                       # a dated public solicitation is at least "today"
        return Priority.P1
    return Priority.P2


def to_revenue_opportunity(qo: QualifiedOpportunity, *, owner: str = "") -> RevenueOpportunity:
    """Map a qualified Discovery opportunity to the Mission-side RevenueOpportunity, preserving lineage.
    The Discovery evidence is referenced (`evidence_ids`), never copied — the chain stays replayable."""
    return RevenueOpportunity(
        opportunity_id=qo.opportunity_id,
        type=OpportunityType.GOV_SOLICITATION,
        source=qo.source,
        summary=f"{qo.issuing_entity}: {qo.title}".strip(": "),
        requested_service=", ".join(qo.categories),
        estimated_value=qo.estimated_value,
        urgency="high" if qo.response_due_at else "normal",
        deadline=qo.response_due_at,
        priority=_priority(qo),
        owner=owner,
        evidence_ids=qo.evidence_ids,                       # references, not copies
        issuing_entity=qo.issuing_entity,
        jurisdiction=qo.jurisdiction_id,
        source_url=qo.source_url,
        geographic_distance_miles=qo.geographic_distance_miles,
        qualification_reasons=tuple(qo.qualification.reasons) + tuple(qo.geographic_reasoning),
        confidence=qo.confidence,
        discovery_digest=opportunity_digest(qo),
    )


# ── idempotent ingest ─────────────────────────────────────────────────────────

class IngestAction(str, Enum):
    CREATED = "CREATED"          # first sighting → a new mission opened
    UNCHANGED = "UNCHANGED"      # rediscovered, no material change → no-op
    AMENDED = "AMENDED"          # material change → existing mission reawakened/updated
    SKIPPED = "SKIPPED"          # not worth a mission (plain REVIEW / REJECT)


@dataclass
class IngestResult:
    action: IngestAction
    opportunity_id: str
    run: Optional[RevenueMissionRun] = None
    note: str = ""


@dataclass
class _Record:
    digest: str
    run: RevenueMissionRun
    opportunity: RevenueOpportunity


class MissionRegistry:
    """Idempotent on Discovery opportunity identity: one opportunity_id → at most one live mission.
    A durable deployment keys this off the event log / a KV; this in-memory map is the P0 reference.
    """

    def __init__(self, *, owner: str, channel_factory: Optional[Callable[[], AttentionChannel]] = None):
        self.owner = owner
        self.channel_factory = channel_factory
        self._by_id: dict[str, _Record] = {}

    def ingest(self, qo: QualifiedOpportunity) -> IngestResult:
        if not should_open(qo):
            return IngestResult(IngestAction.SKIPPED, qo.opportunity_id,
                                note=f"{qo.qualification.decision} — stays in discovery")

        digest = opportunity_digest(qo)
        prev = self._by_id.get(qo.opportunity_id)

        if prev is None:                                    # first sighting
            opp = to_revenue_opportunity(qo, owner=self.owner)
            ch = self.channel_factory() if self.channel_factory else None
            run = RevenueMissionRun(opp, owner=self.owner, channel=ch)
            self._by_id[qo.opportunity_id] = _Record(digest, run, opp)
            return IngestResult(IngestAction.CREATED, qo.opportunity_id, run, "new mission opened")

        if prev.digest == digest:                           # rediscovered, unchanged
            return IngestResult(IngestAction.UNCHANGED, qo.opportunity_id, prev.run,
                                "rediscovered — no material change")

        # material amendment: update the SAME opportunity/mission, don't create a duplicate lead.
        prev.opportunity.deadline = qo.response_due_at or prev.opportunity.deadline
        prev.opportunity.estimated_value = qo.estimated_value
        prev.opportunity.qualification_reasons = (
            tuple(qo.qualification.reasons) + tuple(qo.geographic_reasoning))
        prev.opportunity.discovery_digest = digest
        prev.opportunity.next_action = (
            f"amended {qo.discovered_at or ''}: re-review (deadline {qo.response_due_at or 'n/a'})".strip())
        prev.digest = digest
        note = "amendment — existing mission reawakened"
        # if the owner is still holding the decision, re-deliver an updated brief.
        if prev.run.rt.repo.pending_human(prev.run.mission_id):
            from .gateway import build_brief
            brief = build_brief(prev.opportunity, prev.run.mission_id,
                                approval_policy="amended since first delivered")
            prev.run.gateway.channel.send("[amended]\n" + brief.text)
            note = "amendment — updated brief re-delivered to owner"
        return IngestResult(IngestAction.AMENDED, qo.opportunity_id, prev.run, note)

    def run_for(self, opportunity_id: str) -> Optional[RevenueMissionRun]:
        rec = self._by_id.get(opportunity_id)
        return rec.run if rec else None
