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
import threading
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
            "selected_intervention_id": ev.selected_intervention_id,
            "prior_utility": ev.prior_utility, "tenant_id": ev.tenant_id, "event_id": ev.event_id}


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
        selected_intervention_id=d.get("selected_intervention_id", ""),
        prior_utility=d.get("prior_utility"), tenant_id=d.get("tenant_id"),
        event_id=d.get("event_id", "") or "")


def dedupe_key(ev: OutcomeEvent) -> str:
    """Identity used to make ``append`` idempotent: the explicit ``event_id`` (tenant-scoped), else, for a
    derived outcome, a deterministic id from (observation ids, selected intervention). '' = not dedupable."""
    if ev.event_id:
        return f"{ev.tenant_id or ''}|{ev.event_id}"
    if ev.source_observation_ids and ev.selected_intervention_id:
        return f"{ev.tenant_id or ''}|obs:{','.join(ev.source_observation_ids)}->{ev.selected_intervention_id}"
    return ""


def refuse_simulated(ev: OutcomeEvent) -> None:
    if getattr(ev, "simulated", False):
        raise ValueError("simulated/demo events can never enter an OutcomeStore (no evidence path)")


class OutcomeStore(Protocol):
    def append(self, ev: OutcomeEvent) -> None: ...
    def load(self) -> List[OutcomeEvent]: ...


@dataclass
class InMemoryOutcomeStore:
    """Non-durable store — the default; equivalent to the loop's original in-memory behaviour."""
    events: List[OutcomeEvent] = field(default_factory=list)
    _lock: "threading.Lock" = field(default_factory=threading.Lock, repr=False, compare=False)

    def append(self, ev: OutcomeEvent) -> None:
        refuse_simulated(ev)
        k = dedupe_key(ev)
        with self._lock:                             # check-then-append is atomic
            if k and any(dedupe_key(e) == k for e in self.events):
                return                               # idempotent: same key => no second append
            self.events.append(ev)

    def load(self) -> List[OutcomeEvent]:
        return list(self.events)


_WARNED_NO_FCNTL = False
_PATH_LOCKS: dict = {}
_PATH_LOCKS_GUARD = threading.Lock()


class _flock:
    """Advisory inter-process lock (fcntl.flock); a no-op where fcntl is unavailable."""
    def __init__(self, path: str) -> None:
        self.path, self.fd = path, None

    def __enter__(self):
        try:
            import fcntl
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except ImportError:
            self.fd = None
            global _WARNED_NO_FCNTL
            if not _WARNED_NO_FCNTL:
                _WARNED_NO_FCNTL = True
                import logging
                logging.getLogger(__name__).warning(
                    "fcntl unavailable: FileOutcomeStore has no inter-process lock (single writer only; "
                    "use the Postgres store for concurrent writers)")
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            os.close(self.fd)                        # closing releases the flock


@dataclass
class FileOutcomeStore:
    """Append-only JSONL store: one OutcomeEvent per line, durable across restarts, human-auditable.
    Dependency-free (stdlib json). Safe to point many readers at; a single writer appends."""
    path: str

    def append(self, ev: OutcomeEvent) -> None:
        refuse_simulated(ev)
        k = dedupe_key(ev)
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        # check-then-append is atomic across threads (in-process lock) AND processes (flock on a lock file)
        with _PATH_LOCKS_GUARD:
            tlock = _PATH_LOCKS.setdefault(os.path.abspath(self.path), threading.Lock())
        with tlock, _flock(self.path + ".lock"):
            if k and any(dedupe_key(e) == k for e in self.load()):
                return                               # idempotent: same key => no second append
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, (json.dumps(event_to_dict(ev)) + "\n").encode("utf-8"))
            finally:
                os.close(fd)

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
ALTER TABLE outcome_events ADD COLUMN IF NOT EXISTS prior_utility double precision;
ALTER TABLE outcome_events ADD COLUMN IF NOT EXISTS tenant_id text;
ALTER TABLE outcome_events ADD COLUMN IF NOT EXISTS event_id text NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_oe_event ON outcome_events(coalesce(tenant_id,''), event_id) WHERE event_id <> '';
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
        refuse_simulated(ev)
        eid = ev.event_id
        if not eid:
            k = dedupe_key(ev)
            eid = k.split("|", 1)[1] if k else ""
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO outcome_events
                     (candidate_id, source_app, action, action_kind, observed_reward, reward_dimensions,
                      delay, attribution_confidence, source_observation_ids, candidate_intervention_ids,
                      selected_intervention_id, note, prior_utility, tenant_id, event_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (ev.candidate_id, ev.source_app, ev.action.value if ev.action is not None else None,
                 ev.action_kind, ev.observed_reward, Jsonb(dict(ev.reward_dimensions)), ev.delay,
                 ev.attribution_confidence, list(ev.source_observation_ids),
                 list(ev.candidate_intervention_ids), ev.selected_intervention_id, ev.note, ev.prior_utility, ev.tenant_id, eid))

    def load(self) -> List[OutcomeEvent]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT candidate_id, source_app, action, action_kind, observed_reward, "
                        "reward_dimensions, delay, attribution_confidence, source_observation_ids, "
                        "candidate_intervention_ids, selected_intervention_id, note, prior_utility, "
                        "tenant_id, event_id FROM outcome_events ORDER BY id")
            rows = cur.fetchall()
        out: List[OutcomeEvent] = []
        for (cid, app, action, kind, reward, dims, delay, attr, sobs, cids, sel, note, prior, tenant, eid) in rows:
            if isinstance(dims, str):
                dims = json.loads(dims)
            out.append(OutcomeEvent(
                candidate_id=cid, source_app=app, action=Action(action) if action else None,
                observed_reward=reward, note=note, action_kind=kind,
                reward_dimensions=dict(dims or {}), delay=float(delay),
                attribution_confidence=float(attr), source_observation_ids=tuple(sobs or ()),
                candidate_intervention_ids=tuple(cids or ()), selected_intervention_id=sel or "",
                prior_utility=(float(prior) if prior is not None else None),
                tenant_id=tenant, event_id=eid or ""))
        return out


def load_outcome_log(store: OutcomeStore) -> OutcomeLog:
    """Materialise an OutcomeLog from a store — the loop's substrate, rebuilt from durable storage."""
    return OutcomeLog(events=store.load())


def open_outcome_store(path: Optional[str] = None) -> OutcomeStore:
    """A durable :class:`FileOutcomeStore` at ``path`` (or ``$OUTCOME_STORE_PATH``), else a
    non-durable in-memory store. This is how a deployment opts into persistence."""
    p = path or os.environ.get("OUTCOME_STORE_PATH", "").strip()
    return FileOutcomeStore(p) if p else InMemoryOutcomeStore()
