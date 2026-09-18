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
    opportunity_kind: str = "gov_solicitation"   # gov_solicitation | gov_forecast
    correlation_key: str = ""                # links a forecast ↔ the formal solicitation
    status: str = ""                         # source-declared lifecycle status (from detail enrichment)
    department: str = ""
    place_of_performance_zip: str = ""
    geographic_distance_miles: Optional[float] = None
    categories: tuple[str, ...] = ()
    posted_at: str = ""
    response_due_at: str = ""
    question_due_at: str = ""
    pre_bid_at: str = ""
    estimated_value: Optional[float] = None
    required_licenses: tuple[str, ...] = ()
    required_certifications: tuple[str, ...] = ()
    source_url: str = ""
    evidence_ids: tuple[str, ...] = ()       # references to Discovery evidence
    detail_observed: bool = False
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
            opportunity_kind=d.get("opportunity_kind", "gov_solicitation"),
            correlation_key=d.get("correlation_key", ""), status=d.get("status", ""),
            department=d.get("department", ""),
            place_of_performance_zip=d.get("place_of_performance_zip", ""),
            geographic_distance_miles=d.get("geographic_distance_miles"),
            categories=tuple(d.get("categories", ())), posted_at=d.get("posted_at", ""),
            response_due_at=d.get("response_due_at", ""), question_due_at=d.get("question_due_at", ""),
            pre_bid_at=d.get("pre_bid_at", ""), estimated_value=d.get("estimated_value"),
            required_licenses=tuple(d.get("required_licenses", ())),
            required_certifications=tuple(d.get("required_certifications", ())),
            source_url=d.get("source_url", ""), evidence_ids=tuple(d.get("evidence_ids", ())),
            detail_observed=bool(d.get("detail_observed", False)),
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
        "opportunity_kind": qo.opportunity_kind, "status": qo.status,
        "response_due_at": qo.response_due_at, "estimated_value": qo.estimated_value,
        "required_licenses": sorted(qo.required_licenses),
        "required_certifications": sorted(qo.required_certifications),
        "decision": qo.qualification.decision, "source_url": qo.source_url,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _days_until(iso_date: str, *, today: Optional[str] = None) -> Optional[int]:
    """Whole days from today to an ISO (YYYY-MM-DD) deadline; None if unparseable. Deterministic."""
    from datetime import date
    try:
        y, m, d = (int(x) for x in iso_date[:10].split("-"))
        ref = date.fromisoformat(today) if today else date.today()
        return (date(y, m, d) - ref).days
    except (ValueError, TypeError):
        return None


def _priority(qo: QualifiedOpportunity, *, today: Optional[str] = None) -> Priority:
    """Priority from the REAL deadline, not a flat fallback: within 3 days is act-now (P0), within 14 is
    today (P1), a dated-but-distant tender is scheduled (P2), an undated forecast is a digest item (P3).
    A pre-solicitation forecast is never P0 — it is not yet biddable."""
    days = _days_until(qo.response_due_at, today=today) if qo.response_due_at else None
    if qo.opportunity_kind == "gov_forecast":
        return Priority.P2 if days is not None else Priority.P3
    if days is None:
        return Priority.P2 if qo.response_due_at else Priority.P3
    if days <= 3:
        return Priority.P0
    if days <= 14:
        return Priority.P1
    return Priority.P2


def to_revenue_opportunity(qo: QualifiedOpportunity, *, owner: str = "") -> RevenueOpportunity:
    """Map a qualified Discovery opportunity to the Mission-side RevenueOpportunity, preserving lineage.
    The Discovery evidence is referenced (`evidence_ids`), never copied — the chain stays replayable."""
    is_forecast = qo.opportunity_kind == "gov_forecast"
    otype = OpportunityType.GOV_FORECAST if is_forecast else OpportunityType.GOV_SOLICITATION
    # a forecast is a pre-solicitation heads-up, never itself urgent; a dated solicitation may be
    prefix = "[forecast] " if is_forecast else ""
    return RevenueOpportunity(
        opportunity_id=qo.opportunity_id,
        type=otype,
        source=qo.source,
        summary=f"{prefix}{qo.issuing_entity}: {qo.title}".strip(": "),
        requested_service=", ".join(qo.categories),
        estimated_value=qo.estimated_value,
        urgency=("normal" if is_forecast else ("high" if qo.response_due_at else "normal")),
        deadline=qo.response_due_at,
        priority=_priority(qo),
        owner=owner,
        evidence_ids=qo.evidence_ids,                       # references, not copies
        issuing_entity=qo.issuing_entity,
        department=qo.department,
        status=qo.status,
        jurisdiction=qo.jurisdiction_id,
        source_url=qo.source_url,
        geographic_distance_miles=qo.geographic_distance_miles,
        qualification_reasons=tuple(qo.qualification.reasons) + tuple(qo.geographic_reasoning),
        confidence=qo.confidence,
        discovery_digest=opportunity_digest(qo),
        correlation_key=qo.correlation_key,
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
    run: Optional[RevenueMissionRun]         # None until (re)hydrated — a reloaded index entry is lazy
    opportunity: RevenueOpportunity
    kind: str = "gov_solicitation"
    correlation_key: str = ""
    posted_ref: str = ""                     # posted_at or discovered_at, for forecast-lead-days
    mission_id: str = ""
    store_path: str = ""                     # durable per-mission event log (for restart rehydration)


def _link_lead_days(forecast_ref: str, solicitation_ref: str) -> Optional[int]:
    d = _days_until(solicitation_ref, today=forecast_ref[:10]) if (forecast_ref and solicitation_ref) else None
    return d if (d is not None and d >= 0) else None


class MissionRegistry:
    """Idempotent on Discovery opportunity identity: one opportunity_id → at most one live mission.

    Two things beyond the in-memory dict:
      * **forecast ↔ solicitation linkage** — when a GOV_SOLICITATION arrives whose ``correlation_key``
        matches a previously-seen GOV_FORECAST (or vice versa), the two are linked and the lead time
        (how many days the forecast preceded the formal solicitation) is recorded.
      * **durable restart** — with ``persist_path`` the id→digest/mission index is written append-only and
        reloaded on construction; with ``mission_store_dir`` each mission's events are durable too, so a
        fresh process rediscovers known tenders as UNCHANGED/AMENDED (no duplicate leads) and can
        rehydrate the parked mission the owner was deciding on. Restart + replay are deterministic.
    """

    def __init__(self, *, owner: str, channel_factory: Optional[Callable[[], AttentionChannel]] = None,
                 persist_path: Optional[str] = None, mission_store_dir: Optional[str] = None):
        self.owner = owner
        self.channel_factory = channel_factory
        self.persist_path = persist_path
        self.mission_store_dir = mission_store_dir
        self._by_id: dict[str, _Record] = {}
        self._by_correlation: dict[str, list[str]] = {}
        if persist_path:
            self._load()

    # ── durable index (append-only JSONL) ──
    def _load(self) -> None:
        import os
        if not (self.persist_path and os.path.exists(self.persist_path)):
            return
        with open(self.persist_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                opp = RevenueOpportunity(
                    opportunity_id=r["opportunity_id"],
                    type=OpportunityType(r.get("type", "GOV_SOLICITATION")),
                    source=r.get("source", ""), summary=r.get("summary", ""),
                    deadline=r.get("response_due_at", ""), owner=self.owner,
                    mission_id=r.get("mission_id", ""), evidence_ids=tuple(r.get("evidence_ids", ())),
                    correlation_key=r.get("correlation_key", ""),
                    linked_opportunity_id=r.get("linked_opportunity_id", ""),
                    forecast_lead_days=r.get("forecast_lead_days"),
                    discovery_digest=r["digest"])
                self._by_id[r["opportunity_id"]] = _Record(
                    r["digest"], None, opp, kind=r.get("kind", "gov_solicitation"),
                    correlation_key=r.get("correlation_key", ""), posted_ref=r.get("posted_ref", ""),
                    mission_id=r.get("mission_id", ""), store_path=r.get("store_path", ""))
                if r.get("correlation_key"):
                    self._by_correlation.setdefault(r["correlation_key"], [])
                    if r["opportunity_id"] not in self._by_correlation[r["correlation_key"]]:
                        self._by_correlation[r["correlation_key"]].append(r["opportunity_id"])

    def _persist(self, rec: _Record) -> None:
        if not self.persist_path:
            return
        import os
        os.makedirs(os.path.dirname(os.path.abspath(self.persist_path)), exist_ok=True)
        with open(self.persist_path, "a") as fh:
            fh.write(json.dumps({
                "opportunity_id": rec.opportunity.opportunity_id, "digest": rec.digest,
                "mission_id": rec.mission_id, "kind": rec.kind, "type": rec.opportunity.type.value,
                "correlation_key": rec.correlation_key, "posted_ref": rec.posted_ref,
                "store_path": rec.store_path,
                "summary": rec.opportunity.summary, "source": rec.opportunity.source,
                "response_due_at": rec.opportunity.deadline,
                "evidence_ids": list(rec.opportunity.evidence_ids),
                "linked_opportunity_id": rec.opportunity.linked_opportunity_id,
                "forecast_lead_days": rec.opportunity.forecast_lead_days,
            }, separators=(",", ":")) + "\n")

    def _ensure_run(self, rec: _Record) -> Optional[RevenueMissionRun]:
        """Return the live run, rehydrating it from the durable mission store after a restart if needed."""
        if rec.run is not None or not (rec.mission_id and rec.store_path):
            return rec.run
        import os
        from ..mission.store import EventStore
        if not os.path.exists(rec.store_path):
            return None
        ch = self.channel_factory() if self.channel_factory else None
        rec.run = RevenueMissionRun.rehydrate(rec.opportunity, owner=self.owner,
                                              store=EventStore(path=rec.store_path), channel=ch)
        return rec.run

    # ── forecast ↔ solicitation linkage ──
    def _link_counterpart(self, rec: _Record) -> None:
        key = rec.correlation_key
        if not key:
            return
        for other_id in self._by_correlation.get(key, []):
            other = self._by_id.get(other_id)
            if other is None or other is rec or other.kind == rec.kind:
                continue
            forecast, solicit = (other, rec) if other.kind == "gov_forecast" else (rec, other)
            lead = _link_lead_days(forecast.posted_ref, solicit.posted_ref)
            for a, b in ((rec, other), (other, rec)):
                a.opportunity.linked_opportunity_id = b.opportunity.opportunity_id
            solicit.opportunity.forecast_lead_days = lead
            forecast.opportunity.forecast_lead_days = lead
            break
        self._by_correlation.setdefault(key, [])
        if rec.opportunity.opportunity_id not in self._by_correlation[key]:
            self._by_correlation[key].append(rec.opportunity.opportunity_id)

    def ingest(self, qo: QualifiedOpportunity) -> IngestResult:
        if not should_open(qo):
            return IngestResult(IngestAction.SKIPPED, qo.opportunity_id,
                                note=f"{qo.qualification.decision} — stays in discovery")

        digest = opportunity_digest(qo)
        prev = self._by_id.get(qo.opportunity_id)

        if prev is None:                                    # first sighting
            opp = to_revenue_opportunity(qo, owner=self.owner)
            ch = self.channel_factory() if self.channel_factory else None
            run, store_path = self._open_run(opp, ch)       # durable per-mission log when configured
            rec = _Record(digest, run, opp, kind=qo.opportunity_kind,
                          correlation_key=qo.correlation_key,
                          posted_ref=(qo.posted_at or qo.discovered_at),
                          mission_id=run.mission_id, store_path=store_path)
            self._by_id[qo.opportunity_id] = rec
            self._link_counterpart(rec)
            self._persist(rec)
            note = "new mission opened"
            if opp.linked_opportunity_id:
                note += (f" · linked to {opp.linked_opportunity_id}"
                         + (f" (forecast lead {opp.forecast_lead_days}d)" if opp.forecast_lead_days is not None else ""))
            return IngestResult(IngestAction.CREATED, qo.opportunity_id, run, note)

        if prev.digest == digest:                           # rediscovered, unchanged
            return IngestResult(IngestAction.UNCHANGED, qo.opportunity_id, self._ensure_run(prev),
                                "rediscovered — no material change")

        # material amendment: update the SAME opportunity/mission, don't create a duplicate lead.
        prev.opportunity.deadline = qo.response_due_at or prev.opportunity.deadline
        prev.opportunity.estimated_value = qo.estimated_value
        prev.opportunity.status = qo.status or prev.opportunity.status
        prev.opportunity.qualification_reasons = (
            tuple(qo.qualification.reasons) + tuple(qo.geographic_reasoning))
        prev.opportunity.discovery_digest = digest
        prev.opportunity.next_action = (
            f"amended {qo.discovered_at or ''}: re-review (deadline {qo.response_due_at or 'n/a'})".strip())
        prev.digest = digest
        self._persist(prev)
        note = "amendment — existing mission reawakened"
        # if the owner is still holding the decision, re-deliver an updated brief.
        run = self._ensure_run(prev)
        if run is not None and run.rt.repo.pending_human(run.mission_id):
            from .gateway import build_brief
            brief = build_brief(prev.opportunity, run.mission_id,
                                approval_policy="amended since first delivered")
            run.gateway.channel.send("[amended]\n" + brief.text)
            note = "amendment — updated brief re-delivered to owner"
        return IngestResult(IngestAction.AMENDED, qo.opportunity_id, run, note)

    def _open_run(self, opp: RevenueOpportunity, ch) -> tuple[RevenueMissionRun, str]:
        """Open a mission. With ``mission_store_dir`` its events persist to a durable per-mission file
        (returned so the index can find it for restart rehydration); otherwise an in-memory store."""
        if not self.mission_store_dir:
            return RevenueMissionRun(opp, owner=self.owner, channel=ch), ""
        import os
        import uuid
        from ..mission.store import EventStore
        os.makedirs(self.mission_store_dir, exist_ok=True)
        mission_file = os.path.join(self.mission_store_dir, f"m-{uuid.uuid4().hex[:12]}.jsonl")
        run = RevenueMissionRun(opp, owner=self.owner, channel=ch, store=EventStore(path=mission_file))
        return run, mission_file

    def run_for(self, opportunity_id: str) -> Optional[RevenueMissionRun]:
        rec = self._by_id.get(opportunity_id)
        return self._ensure_run(rec) if rec else None

    def linkage(self, opportunity_id: str) -> Optional[dict]:
        """The forecast↔solicitation link for an opportunity, if any: counterpart id + forecast lead days."""
        rec = self._by_id.get(opportunity_id)
        if rec is None or not rec.opportunity.linked_opportunity_id:
            return None
        return {"linked_opportunity_id": rec.opportunity.linked_opportunity_id,
                "forecast_lead_days": rec.opportunity.forecast_lead_days,
                "correlation_key": rec.correlation_key}
