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
            "attribution_confidence": ev.attribution_confidence}


def event_from_dict(d: dict) -> OutcomeEvent:
    raw_action = d.get("action")
    return OutcomeEvent(
        candidate_id=d.get("candidate_id", ""), source_app=d.get("source_app", ""),
        action=Action(raw_action) if raw_action else None, accepted=d.get("accepted"),
        edited=d.get("edited", False), observed_reward=d.get("observed_reward"),
        note=d.get("note", ""), action_kind=d.get("action_kind", ""),
        reward_dimensions=dict(d.get("reward_dimensions") or {}), delay=d.get("delay", 0.0),
        attribution_confidence=d.get("attribution_confidence", 1.0))


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


def load_outcome_log(store: OutcomeStore) -> OutcomeLog:
    """Materialise an OutcomeLog from a store — the loop's substrate, rebuilt from durable storage."""
    return OutcomeLog(events=store.load())


def open_outcome_store(path: Optional[str] = None) -> OutcomeStore:
    """A durable :class:`FileOutcomeStore` at ``path`` (or ``$OUTCOME_STORE_PATH``), else a
    non-durable in-memory store. This is how a deployment opts into persistence."""
    p = path or os.environ.get("OUTCOME_STORE_PATH", "").strip()
    return FileOutcomeStore(p) if p else InMemoryOutcomeStore()
