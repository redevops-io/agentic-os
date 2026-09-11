"""InterventionRecord — the immutable, event-sourced record of every decision the runtime made
(REAL_DATA_PROVIDER_SCOPING.md v2 §3.3).

Promoted from an implementation detail to a first-class requirement: without persisting the decision
CONTEXT (opportunity, chosen action, alternatives, evidence, policy version, score, timestamps),
six months later you know an outcome occurred but cannot reconstruct what it should be learned against.

Records are **immutable** and append-only. Correlating an outcome to an intervention does NOT mutate
the record — it appends a separate `OutcomeLink` event; the "current" outcome set for an intervention
is a projection (a join) over those events. That keeps the history event-sourced and replayable, not a
mutable "current intervention" blob.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Tuple

from agentic_os.priority_engine import (
    DecisionOpportunity, PriorityPolicy, SelectedAction, UtilityFn, select_action)


@dataclass(frozen=True)
class InterventionRecord:
    intervention_id: str
    opportunity_id: str
    candidate_id: str
    selected_action: str                         # the chosen action_kind / label
    alternatives: Tuple[Tuple[str, float], ...]  # (label, utility) runners-up — the decision context
    evidence_refs: Tuple[str, ...]
    policy_version: str
    score: float                                 # the expected utility the selection was made on
    proposed_at: float
    approved_at: Optional[float] = None
    executed_at: Optional[float] = None
    execution_ref: str = ""
    mission_id: str = ""


@dataclass(frozen=True)
class OutcomeLink:
    """An append-only correlation event: this observed outcome is attributed to this intervention.
    Kept separate from the InterventionRecord so records stay immutable."""
    intervention_id: str
    outcome_ref: str                             # id of the OutcomeEvent / observation
    attribution_confidence: float
    linked_at: float


def record_from_selection(sel: SelectedAction, *, intervention_id: str, policy_version: str,
                          proposed_at: float, evidence_refs: Tuple[str, ...] = (),
                          approved_at: Optional[float] = None, executed_at: Optional[float] = None,
                          execution_ref: str = "", mission_id: str = "") -> InterventionRecord:
    """Build the immutable record from a Priority-Engine SelectedAction — capturing the alternatives and
    score so the decision is reconstructable later."""
    c = sel.action
    return InterventionRecord(
        intervention_id=intervention_id, opportunity_id=sel.opportunity_id,
        candidate_id=c.candidate_id, selected_action=(c.action_kind or c.proposed_action[:40]),
        alternatives=tuple(sel.alternatives), evidence_refs=tuple(evidence_refs),
        policy_version=policy_version, score=sel.expected_utility, proposed_at=proposed_at,
        approved_at=approved_at, executed_at=executed_at, execution_ref=execution_ref,
        mission_id=mission_id)


# ── stores (append-only; in-memory + durable JSONL, mirroring OutcomeStore) ──────────
class InterventionStore(Protocol):
    def append(self, rec: InterventionRecord) -> None: ...
    def link(self, link: OutcomeLink) -> None: ...
    def all(self) -> List[InterventionRecord]: ...
    def links(self) -> List[OutcomeLink]: ...

    def outcome_refs(self, intervention_id: str) -> List[str]: ...


def _rec_to_dict(r: InterventionRecord) -> dict:
    return {"_t": "rec", "intervention_id": r.intervention_id, "opportunity_id": r.opportunity_id,
            "candidate_id": r.candidate_id, "selected_action": r.selected_action,
            "alternatives": [list(a) for a in r.alternatives], "evidence_refs": list(r.evidence_refs),
            "policy_version": r.policy_version, "score": r.score, "proposed_at": r.proposed_at,
            "approved_at": r.approved_at, "executed_at": r.executed_at,
            "execution_ref": r.execution_ref, "mission_id": r.mission_id}


def _rec_from_dict(d: dict) -> InterventionRecord:
    return InterventionRecord(
        intervention_id=d["intervention_id"], opportunity_id=d.get("opportunity_id", ""),
        candidate_id=d.get("candidate_id", ""), selected_action=d.get("selected_action", ""),
        alternatives=tuple(tuple(a) for a in d.get("alternatives", [])),
        evidence_refs=tuple(d.get("evidence_refs", [])), policy_version=d.get("policy_version", ""),
        score=d.get("score", 0.0), proposed_at=d.get("proposed_at", 0.0),
        approved_at=d.get("approved_at"), executed_at=d.get("executed_at"),
        execution_ref=d.get("execution_ref", ""), mission_id=d.get("mission_id", ""))


def _link_to_dict(l: OutcomeLink) -> dict:
    return {"_t": "link", "intervention_id": l.intervention_id, "outcome_ref": l.outcome_ref,
            "attribution_confidence": l.attribution_confidence, "linked_at": l.linked_at}


@dataclass
class InMemoryInterventionStore:
    records: List[InterventionRecord] = field(default_factory=list)
    _links: List[OutcomeLink] = field(default_factory=list)

    def append(self, rec: InterventionRecord) -> None:
        self.records.append(rec)

    def link(self, link: OutcomeLink) -> None:
        self._links.append(link)

    def all(self) -> List[InterventionRecord]:
        return list(self.records)

    def links(self) -> List[OutcomeLink]:
        return list(self._links)

    def outcome_refs(self, intervention_id: str) -> List[str]:
        return [l.outcome_ref for l in self._links if l.intervention_id == intervention_id]


@dataclass
class FileInterventionStore:
    """Durable append-only JSONL store — records and outcome links interleaved (tagged `_t`), so the
    file is the event log. Survives restart; corrupt tail lines are skipped."""
    path: str

    def _append_line(self, obj: dict) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")

    def append(self, rec: InterventionRecord) -> None:
        self._append_line(_rec_to_dict(rec))

    def link(self, link: OutcomeLink) -> None:
        self._append_line(_link_to_dict(link))

    def _read(self):
        recs: List[InterventionRecord] = []
        links: List[OutcomeLink] = []
        if not os.path.exists(self.path):
            return recs, links
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("_t") == "link":
                        links.append(OutcomeLink(d["intervention_id"], d["outcome_ref"],
                                                 d["attribution_confidence"], d["linked_at"]))
                    else:
                        recs.append(_rec_from_dict(d))
                except (ValueError, KeyError):
                    continue
        return recs, links

    def all(self) -> List[InterventionRecord]:
        return self._read()[0]

    def links(self) -> List[OutcomeLink]:
        return self._read()[1]

    def outcome_refs(self, intervention_id: str) -> List[str]:
        return [l.outcome_ref for l in self._read()[1] if l.intervention_id == intervention_id]


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS interventions (
  intervention_id  text PRIMARY KEY,
  opportunity_id   text NOT NULL,
  candidate_id     text NOT NULL,
  selected_action  text NOT NULL,
  alternatives     jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_refs    text[] NOT NULL DEFAULT '{}',
  policy_version   text NOT NULL,
  score            double precision NOT NULL,
  proposed_at      double precision NOT NULL,
  approved_at      double precision,
  executed_at      double precision,
  execution_ref    text NOT NULL DEFAULT '',
  mission_id       text NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outcome_links (
  id                      bigserial PRIMARY KEY,
  intervention_id         text NOT NULL REFERENCES interventions(intervention_id),
  outcome_ref             text NOT NULL,
  attribution_confidence  double precision NOT NULL,
  linked_at               double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_link_iv ON outcome_links(intervention_id);
"""


class PostgresInterventionStore:
    """The operational, durable InterventionStore (Postgres). Records are IMMUTABLE — ``append`` uses
    ON CONFLICT DO NOTHING, so a recommendation's decision context is never overwritten; correlation is
    a separate append into ``outcome_links``. ``psycopg`` (v3) is imported lazily; DSN from
    ``$OBS_DATABASE_URL`` (the same operational database that holds observations)."""

    def __init__(self, dsn: Optional[str] = None, *, ensure_schema: bool = True) -> None:
        from agentic_os.observation_store import observation_dsn
        resolved = observation_dsn(dsn)
        if not resolved:
            raise ValueError("no Postgres DSN (pass dsn= or set OBS_DATABASE_URL)")
        import psycopg
        self._conn = psycopg.connect(resolved, autocommit=True)
        if ensure_schema:
            with self._conn.cursor() as cur:
                cur.execute(_PG_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def append(self, rec: InterventionRecord) -> None:
        from psycopg.types.json import Jsonb
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO interventions
                     (intervention_id, opportunity_id, candidate_id, selected_action, alternatives,
                      evidence_refs, policy_version, score, proposed_at, approved_at, executed_at,
                      execution_ref, mission_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (intervention_id) DO NOTHING""",   # immutable — never overwrite
                (rec.intervention_id, rec.opportunity_id, rec.candidate_id, rec.selected_action,
                 Jsonb([list(a) for a in rec.alternatives]), list(rec.evidence_refs),
                 rec.policy_version, rec.score, rec.proposed_at, rec.approved_at, rec.executed_at,
                 rec.execution_ref, rec.mission_id))

    def link(self, link: OutcomeLink) -> None:
        with self._conn.cursor() as cur:
            cur.execute("INSERT INTO outcome_links (intervention_id, outcome_ref, attribution_confidence,"
                        " linked_at) VALUES (%s,%s,%s,%s)",
                        (link.intervention_id, link.outcome_ref, link.attribution_confidence, link.linked_at))

    def get(self, intervention_id: str) -> Optional[InterventionRecord]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_REC + " WHERE intervention_id = %s", (intervention_id,))
            row = cur.fetchone()
        return _pg_row_to_rec(row) if row else None

    def all(self) -> List[InterventionRecord]:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_REC + " ORDER BY proposed_at, intervention_id")
            return [_pg_row_to_rec(r) for r in cur.fetchall()]

    def links(self) -> List[OutcomeLink]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT intervention_id, outcome_ref, attribution_confidence, linked_at "
                        "FROM outcome_links ORDER BY id")
            return [OutcomeLink(*r) for r in cur.fetchall()]

    def outcome_refs(self, intervention_id: str) -> List[str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT outcome_ref FROM outcome_links WHERE intervention_id = %s ORDER BY id",
                        (intervention_id,))
            return [r[0] for r in cur.fetchall()]


_SELECT_REC = ("SELECT intervention_id, opportunity_id, candidate_id, selected_action, alternatives, "
               "evidence_refs, policy_version, score, proposed_at, approved_at, executed_at, "
               "execution_ref, mission_id FROM interventions")


def _pg_row_to_rec(row) -> InterventionRecord:
    (iid, oid, cid, act, alts, refs, pv, score, prop, appr, exe, exref, mid) = row
    if isinstance(alts, str):
        alts = json.loads(alts)
    return InterventionRecord(
        intervention_id=iid, opportunity_id=oid, candidate_id=cid, selected_action=act,
        alternatives=tuple(tuple(a) for a in (alts or [])), evidence_refs=tuple(refs or ()),
        policy_version=pv, score=float(score), proposed_at=float(prop),
        approved_at=(float(appr) if appr is not None else None),
        executed_at=(float(exe) if exe is not None else None), execution_ref=exref or "", mission_id=mid or "")


def select_and_record(opportunity: DecisionOpportunity, store: "InterventionStore", *,
                      policy_version: str, proposed_at: float, policy: Optional[PriorityPolicy] = None,
                      utility_fn: Optional[UtilityFn] = None, evidence_refs: Tuple[str, ...] = (),
                      id_fn: Optional[Callable[[], str]] = None) -> Tuple[SelectedAction, InterventionRecord]:
    """Select an action for an opportunity AND persist the decision before it can be surfaced. This is
    the boundary that makes 'every recommendation durable before it's shown' true — INCLUDING a WAIT or
    a DO_NOT_CONTACT / abstain: the record is written for whatever was chosen, not only for actions
    taken. Returns the selection and the immutable record now in the store."""
    sel = select_action(opportunity, policy, utility_fn=utility_fn)
    intervention_id = (id_fn or (lambda: uuid.uuid4().hex))()
    rec = record_from_selection(sel, intervention_id=intervention_id, policy_version=policy_version,
                                proposed_at=proposed_at, evidence_refs=evidence_refs)
    store.append(rec)                                    # durable BEFORE the caller surfaces `sel`
    return sel, rec
