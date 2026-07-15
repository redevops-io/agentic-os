"""Event-sourced stores — the source of truth the whole kernel agrees on.

Everything is an append-only event; state is a fold over events. That buys a free audit
trail, crash recovery (restart-resume), and replay for learning. One `EventStore` (in-memory
+ optional JSONL persistence) backs repositories that rebuild missions, plans and world state.
In production the JSONL sink becomes Postgres + the Kafka bus; the fold logic is unchanged.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .types import (
    Mission, MissionState, ExecutionPlan, ExecutionGraph, Node, NodeState,
    Belief, HumanTask, now, new_id, to_jsonable,
)


@dataclass
class Event:
    type: str
    mission_id: str
    payload: dict[str, Any]
    seq: int = 0
    ts: float = field(default_factory=now)


class EventStore:
    """Append-only log. In-memory list, optionally persisted to a JSONL file and reloaded on
    startup so a restart can rebuild every mission's state (the durability guarantee)."""

    def __init__(self, path: str | None = None):
        self._events: list[Event] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[Event], None]] = []
        self._path = path
        if path and os.path.exists(path):
            self._load()

    def _load(self) -> None:
        with open(self._path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                ev = Event(type=d["type"], mission_id=d["mission_id"], payload=d["payload"],
                           seq=d["seq"], ts=d["ts"])
                self._events.append(ev)
                self._seq = max(self._seq, ev.seq)

    def append(self, type: str, mission_id: str, payload: dict[str, Any]) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(type=type, mission_id=mission_id, payload=to_jsonable(payload), seq=self._seq)
            self._events.append(ev)
            if self._path:
                with open(self._path, "a") as fh:
                    fh.write(json.dumps({"seq": ev.seq, "ts": ev.ts, "type": ev.type,
                                         "mission_id": ev.mission_id, "payload": ev.payload}) + "\n")
        for cb in list(self._subscribers):
            try:
                cb(ev)
            except Exception:  # a subscriber must never break the log
                pass
        return ev

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self._subscribers.append(cb)

    def for_mission(self, mission_id: str) -> list[Event]:
        return [e for e in self._events if e.mission_id == mission_id]

    def all(self) -> list[Event]:
        return list(self._events)

    def mission_ids(self) -> list[str]:
        seen: list[str] = []
        for e in self._events:
            if e.mission_id and e.mission_id not in seen:
                seen.append(e.mission_id)
        return seen


# ─── world state (the blackboard) ────────────────────────────────────────────
class WorldState:
    """An event-sourced blackboard for one mission. Capabilities read facts and write
    OBSERVATIONS; observations are fused into confidence-weighted BELIEFS (see belief.py)
    before they become the fact a downstream capability reads."""

    def __init__(self, mission_id: str, store: EventStore, fuse: Callable[[list[dict]], Belief]):
        self.mission_id = mission_id
        self._store = store
        self._fuse = fuse

    def _observations(self, key: str) -> list[dict]:
        obs = []
        for e in self._store.for_mission(self.mission_id):
            if e.type == "ObservationWritten" and e.payload.get("key") == key:
                obs.append(e.payload)
        return obs

    def observe(self, key: str, value: Any, source: str, confidence: float = 1.0) -> None:
        self._store.append("ObservationWritten", self.mission_id,
                            {"key": key, "value": value, "source": source, "confidence": confidence})

    def belief(self, key: str) -> Belief | None:
        obs = self._observations(key)
        if not obs:
            return None
        return self._fuse(obs)

    def get(self, key: str, default: Any = None) -> Any:
        b = self.belief(key)
        return b.value if b is not None else default

    def snapshot(self) -> dict[str, Belief]:
        keys = []
        for e in self._store.for_mission(self.mission_id):
            if e.type == "ObservationWritten":
                k = e.payload.get("key")
                if k and k not in keys:
                    keys.append(k)
        return {k: self.belief(k) for k in keys}


# ─── repositories (fold events -> current state) ─────────────────────────────
class MissionRepository:
    def __init__(self, store: EventStore):
        self.store = store

    def state(self, mission_id: str) -> MissionState | None:
        st: MissionState | None = None
        for e in self.store.for_mission(mission_id):
            if e.type == "MissionCreated":
                st = MissionState.PLANNING
            elif e.type == "MissionStateChanged":
                st = MissionState(e.payload["state"])
        return st

    def active_plan_id(self, mission_id: str) -> str | None:
        pid = None
        for e in self.store.for_mission(mission_id):
            if e.type == "PlanActivated":
                pid = e.payload["plan_id"]
        return pid

    def goal(self, mission_id: str) -> str | None:
        for e in self.store.for_mission(mission_id):
            if e.type == "MissionCreated":
                return e.payload.get("goal")
        return None

    def node_status(self, mission_id: str) -> dict[str, NodeState]:
        """Current status of every node this mission has touched (fold of node events)."""
        status: dict[str, NodeState] = {}
        for e in self.store.for_mission(mission_id):
            if e.type in ("NodeDispatched", "NodeSucceeded", "NodeFailed", "NodeParked",
                          "NodeResumed", "NodeCompensated", "NodeSkipped"):
                nid = e.payload["node_id"]
                status[nid] = {
                    "NodeDispatched": NodeState.RUNNING,
                    "NodeSucceeded": NodeState.DONE,
                    "NodeFailed": NodeState.FAILED,
                    "NodeParked": NodeState.WAITING,
                    "NodeResumed": NodeState.RUNNING,
                    "NodeCompensated": NodeState.COMPENSATED,
                    "NodeSkipped": NodeState.SKIPPED,
                }[e.type]
        return status

    def node_results(self, mission_id: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in self.store.for_mission(mission_id):
            if e.type == "NodeSucceeded":
                out[e.payload["node_id"]] = e.payload.get("result") or {}
        return out

    def pending_human(self, mission_id: str) -> dict | None:
        """The open human task, if the mission is parked (fold: parked minus resolved)."""
        parked: dict[str, dict] = {}
        for e in self.store.for_mission(mission_id):
            if e.type == "NodeParked":
                parked[e.payload["node_id"]] = {"capability": e.payload.get("capability"),
                                                **(e.payload.get("human_task") or {})}
            elif e.type in ("NodeResumed", "NodeSkipped"):
                parked.pop(e.payload["node_id"], None)
        if not parked:
            return None
        nid, task = next(iter(parked.items()))
        return {"node_id": nid, **task}

    def timeline(self, mission_id: str) -> list[dict]:
        return [{"seq": e.seq, "ts": e.ts, "type": e.type, "payload": e.payload}
                for e in self.store.for_mission(mission_id)]

    def created(self, mission_id: str) -> dict:
        for e in self.store.for_mission(mission_id):
            if e.type == "MissionCreated":
                return e.payload
        return {}

    def list_missions(self) -> list[dict]:
        """Every mission the store knows about (durable across restarts) — for the cockpit list."""
        out = []
        for mid in self.store.mission_ids():
            st = self.state(mid)
            c = self.created(mid)
            out.append({"id": mid, "goal": c.get("goal"), "template": c.get("template"),
                        "state": st.value if st else None})
        return out
