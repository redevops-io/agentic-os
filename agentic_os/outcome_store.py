"""Durable outcome storage — the persistence the learning loop needs to survive a restart and to be
fed by real data over time (AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §20).

The in-memory OutcomeLog proves the loop; a deployment needs the record to DURABLE. This is the seam:
an :class:`OutcomeStore` (append + load), with an in-memory implementation and a dependency-free
append-only JSONL file implementation. Because the learner is a pure function of the log, persisting
the log is all it takes for learned selection to survive a restart — and it keeps the loop replayable
(the file IS the replay tape) and auditable (a human-readable record of every observed outcome).

Serialisation is explicit and total over the OutcomeEvent contract (the governance Action is stored as
its ``.value`` or null); an unreadable line is skipped rather than crashing the load, so a partially
written tail (a crash mid-append) never bricks the loop.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Protocol

from agentic_os.priority_engine import Action, OutcomeEvent, OutcomeLog


def event_to_dict(ev: OutcomeEvent) -> dict:
    return {"candidate_id": ev.candidate_id, "source_app": ev.source_app,
            "action": ev.action.value if ev.action is not None else None,
            "accepted": ev.accepted, "edited": ev.edited, "observed_reward": ev.observed_reward,
            "note": ev.note, "action_kind": ev.action_kind,
            "reward_dimensions": dict(ev.reward_dimensions), "delay": ev.delay,
            "attribution_confidence": ev.attribution_confidence,
            "source_observation_ids": list(ev.source_observation_ids),
            "candidate_intervention_ids": list(ev.candidate_intervention_ids),
            "selected_intervention_id": ev.selected_intervention_id}


def event_from_dict(d: dict) -> OutcomeEvent:
    raw_action = d.get("action")
    return OutcomeEvent(
        candidate_id=d.get("candidate_id", ""), source_app=d.get("source_app", ""),
        action=Action(raw_action) if raw_action else None, accepted=d.get("accepted"),
        edited=d.get("edited", False), observed_reward=d.get("observed_reward"),
        note=d.get("note", ""), action_kind=d.get("action_kind", ""),
        reward_dimensions=dict(d.get("reward_dimensions") or {}), delay=d.get("delay", 0.0),
        attribution_confidence=d.get("attribution_confidence", 1.0),
        source_observation_ids=tuple(d.get("source_observation_ids") or ()),
        candidate_intervention_ids=tuple(d.get("candidate_intervention_ids") or ()),
        selected_intervention_id=d.get("selected_intervention_id", ""))


class OutcomeStore(Protocol):
    def append(self, ev: OutcomeEvent) -> None: ...
    def load(self) -> List[OutcomeEvent]: ...


@dataclass
class InMemoryOutcomeStore:
    """Non-durable store — the default; equivalent to the loop's original in-memory behaviour."""
    events: List[OutcomeEvent] = field(default_factory=list)

    def append(self, ev: OutcomeEvent) -> None:
        self.events.append(ev)

    def load(self) -> List[OutcomeEvent]:
        return list(self.events)


@dataclass
class FileOutcomeStore:
    """Append-only JSONL store: one OutcomeEvent per line, durable across restarts, human-auditable.
    Dependency-free (stdlib json). Safe to point many readers at; a single writer appends."""
    path: str

    def append(self, ev: OutcomeEvent) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event_to_dict(ev)) + "\n")

    def load(self) -> List[OutcomeEvent]:
        if not os.path.exists(self.path):
            return []
        out: List[OutcomeEvent] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(event_from_dict(json.loads(line)))
                except (ValueError, KeyError):
                    continue          # skip a corrupt/partial tail line rather than brick the loop
        return out


_PG_OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcome_events (
  id                          bigserial PRIMARY KEY,
  candidate_id                text NOT NULL,
  source_app                  text NOT NULL,
  action                      text,
  action_kind                 text NOT NULL DEFAULT '',
  observed_reward             double precision,
  reward_dimensions           jsonb NOT NULL DEFAULT '{}'::jsonb,
  delay                       double precision NOT NULL DEFAULT 0,
  attribution_confidence      double precision NOT NULL DEFAULT 1,
  source_observation_ids      text[] NOT NULL DEFAULT '{}',
  candidate_intervention_ids  text[] NOT NULL DEFAULT '{}',
  selected_intervention_id    text NOT NULL DEFAULT '',
  note                        text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_oe_selected ON outcome_events(selected_intervention_id);
CREATE INDEX IF NOT EXISTS ix_oe_key ON outcome_events(source_app, action_kind);
"""


class PostgresOutcomeStore:
    """Durable, queryable outcome store (Postgres) — the operational home for DERIVED OutcomeEvents,
    with the causal-graph edges (source_observation_ids / candidate_intervention_ids /
    selected_intervention_id) kept as columns so EXPLAIN can trace them. Implements the OutcomeStore
    contract (append + load). ``psycopg`` lazy; DSN from ``$OBS_DATABASE_URL``."""

    def __init__(self, dsn: Optional[str] = None, *, ensure_schema: bool = True) -> None:
        resolved = (dsn or os.environ.get("OBS_DATABASE_URL") or os.environ.get("DATABASE_URL") or None)
        if not resolved:
            raise ValueError("no Postgres DSN (pass dsn= or set OBS_DATABASE_URL)")
        import psycopg
        self._conn = psycopg.connect(resolved, autocommit=True)
        if ensure_schema:
            with self._conn.cursor() as cur:
                cur.execute(_PG_OUTCOME_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def append(self, ev: OutcomeEvent) -> None:
        from psycopg.types.json import Jsonb
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO outcome_events
                     (candidate_id, source_app, action, action_kind, observed_reward, reward_dimensions,
                      delay, attribution_confidence, source_observation_ids, candidate_intervention_ids,
                      selected_intervention_id, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ev.candidate_id, ev.source_app, ev.action.value if ev.action is not None else None,
                 ev.action_kind, ev.observed_reward, Jsonb(dict(ev.reward_dimensions)), ev.delay,
                 ev.attribution_confidence, list(ev.source_observation_ids),
                 list(ev.candidate_intervention_ids), ev.selected_intervention_id, ev.note))

    def load(self) -> List[OutcomeEvent]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT candidate_id, source_app, action, action_kind, observed_reward, "
                        "reward_dimensions, delay, attribution_confidence, source_observation_ids, "
                        "candidate_intervention_ids, selected_intervention_id, note "
                        "FROM outcome_events ORDER BY id")
            rows = cur.fetchall()
        out: List[OutcomeEvent] = []
        for (cid, app, action, kind, reward, dims, delay, attr, sobs, cids, sel, note) in rows:
            if isinstance(dims, str):
                dims = json.loads(dims)
            out.append(OutcomeEvent(
                candidate_id=cid, source_app=app, action=Action(action) if action else None,
                observed_reward=reward, note=note, action_kind=kind,
                reward_dimensions=dict(dims or {}), delay=float(delay),
                attribution_confidence=float(attr), source_observation_ids=tuple(sobs or ()),
                candidate_intervention_ids=tuple(cids or ()), selected_intervention_id=sel or ""))
        return out


def load_outcome_log(store: OutcomeStore) -> OutcomeLog:
    """Materialise an OutcomeLog from a store — the loop's substrate, rebuilt from durable storage."""
    return OutcomeLog(events=store.load())


def open_outcome_store(path: Optional[str] = None) -> OutcomeStore:
    """A durable :class:`FileOutcomeStore` at ``path`` (or ``$OUTCOME_STORE_PATH``), else a
    non-durable in-memory store. This is how a deployment opts into persistence."""
    p = path or os.environ.get("OUTCOME_STORE_PATH", "").strip()
    return FileOutcomeStore(p) if p else InMemoryOutcomeStore()
