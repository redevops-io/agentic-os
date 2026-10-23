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
from typing import Any, Callable, Protocol, runtime_checkable

from .types import (
    Mission, MissionState, ExecutionPlan, ExecutionGraph, Node, NodeState,
    Belief, HumanTask, now, new_id, to_jsonable,
)


def _fsync_enabled() -> bool:
    """fsync after each append (default on). ``AGENTIC_OS_EVENT_FSYNC=0`` trades durability for speed (tests)."""
    return os.environ.get("AGENTIC_OS_EVENT_FSYNC", "1").strip().lower() not in ("0", "false", "no", "off")


def _mission_index(store) -> "dict[str, list[Event]]":
    """Per-mission view of ``store._events``, built incrementally (each event is indexed once), so lookups and
    ``mission_ids`` stay linear overall instead of rescanning the whole log per call."""
    lock = store.__dict__.setdefault("_idx_lock", threading.Lock())
    with lock:
        idx = store.__dict__.setdefault("_idx", {})
        n = store.__dict__.get("_idx_n", 0)
        events = store._events
        if n > len(events):                       # the log was replaced/truncated: rebuild
            idx.clear()
            n = 0
        for e in events[n:]:
            idx.setdefault(e.mission_id, []).append(e)
        store._idx_n = len(events)
        return idx


@runtime_checkable
class EventStoreProtocol(Protocol):
    """The append-only event-log interface the kernel depends on. Implementations are interchangeable so long
    as append/fold semantics match: ``EventStore`` (a.k.a. ``JsonlEventStore``) for a durable log (in-memory +
    optional JSONL sink, Postgres in production), ``MemoryEventStore`` for fast, isolated in-memory replay in
    tests. Runtime actors type their dependency as this Protocol, never a concrete backend."""

    def append(self, type: str, mission_id: str, payload: dict[str, Any]) -> "Event": ...
    def subscribe(self, cb: Callable[["Event"], None]) -> None: ...
    def for_mission(self, mission_id: str) -> list["Event"]: ...
    def all(self) -> list["Event"]: ...
    def mission_ids(self) -> list[str]: ...


@dataclass
class Event:
    type: str
    mission_id: str
    payload: dict[str, Any]
    seq: int = 0
    ts: float = field(default_factory=now)
    prev: str = ""      # entry hash of the previous event of the SAME mission ("" for the first)
    hash: str = ""      # sha256(prev || canonical(seq, ts, type, mission_id, payload)): tamper evidence


def _canonical_entry(seq: int, ts: float, type: str, mission_id: str, payload: Any) -> bytes:
    return json.dumps({"seq": seq, "ts": ts, "type": type, "mission_id": mission_id, "payload": payload},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def entry_hash(prev: str, seq: int, ts: float, type: str, mission_id: str, payload: Any) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(prev.encode("ascii") + b"|" + _canonical_entry(seq, ts, type, mission_id, payload)).hexdigest()


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int            # hashed events verified
    unchained: int          # legacy events with no hash (not covered)
    first_bad_seq: int = 0
    reason: str = ""


def verify_chain(events: "list[Event]") -> ChainReport:
    """Check each mission's hash chain: every hashed event must hash correctly and link to its predecessor, so an
    edited, deleted or reordered event is detected. Events written before chaining (hash == "") are counted as
    unchained and not covered. Truncating the END of a log is not detectable from the log alone: anchor the head
    (``mission_receipt['ledger_head']``) somewhere the log's writer cannot edit."""
    heads: dict[str, str] = {}
    checked = unchained = 0
    for ev in events:
        # an event from a store that predates chaining (the enterprise overlay carries a fork of this module whose
        # Event has no hash/prev attributes at all) is "unchained", never an AttributeError
        if not getattr(ev, "hash", ""):
            unchained += 1
            continue
        want_prev = heads.get(ev.mission_id, "")
        if getattr(ev, "prev", "") != want_prev:
            return ChainReport(False, checked, unchained, ev.seq, "an event is missing, reordered or replaced")
        if entry_hash(ev.prev, ev.seq, ev.ts, ev.type, ev.mission_id, ev.payload) != ev.hash:
            return ChainReport(False, checked, unchained, ev.seq, "an event was modified")
        heads[ev.mission_id] = ev.hash
        checked += 1
    return ChainReport(True, checked, unchained)


class EventStore:
    """Append-only log. In-memory list, optionally persisted to a JSONL file and reloaded on
    startup so a restart can rebuild every mission's state (the durability guarantee)."""

    def __init__(self, path: str | None = None):
        self._events: list[Event] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[Event], None]] = []
        self._path = path
        self._heads: dict[str, str] = {}     # mission_id -> hash of its last event (the chain head)
        self._offset = 0            # bytes of the JSONL already folded into memory
        if path and os.path.exists(path):
            with self._lock:
                self._read_tail()

    def _read_tail(self) -> int:
        """Fold records written past ``_offset`` (by any process) into memory. A final line without a newline is
        accepted when it is a complete record, and left alone when it is a torn write. Caller holds ``_lock``."""
        if not self._path or not os.path.exists(self._path):
            return 0
        added = 0
        with open(self._path, "rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
        end = data.rfind(b"\n") + 1
        tail = data[end:]
        chunks = list(data[:end].splitlines())
        consumed = end
        if tail.strip():
            try:
                json.loads(tail.decode("utf-8"))
                chunks.append(tail)
                consumed = len(data)
            except ValueError:
                pass                                    # torn write: ignored here, trimmed by the next append
        for line in chunks:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line.decode("utf-8"))
            self._events.append(Event(type=d["type"], mission_id=d["mission_id"], payload=d["payload"],
                                      seq=d["seq"], ts=d["ts"], prev=d.get("prev", ""), hash=d.get("hash", "")))
            if d.get("hash"):
                self._heads[d["mission_id"]] = d["hash"]
            self._seq = max(self._seq, d["seq"])
            added += 1
        self._offset += consumed
        return added

    def _load(self) -> None:
        with self._lock:
            self._read_tail()

    def refresh(self) -> int:
        """Pick up events another process appended to the shared JSONL since this store last looked
        (fork/join driver claims use it before deciding who may drive). Returns the number added."""
        with self._lock:
            return self._read_tail()

    def append(self, type: str, mission_id: str, payload: dict[str, Any]) -> Event:
        with self._lock:
            if self._path:
                # The exclusive inter-process lock is taken on the log file itself (no sidecar file), so
                # read-only parent directories and single-file bind mounts work.
                with open(self._path, "ab") as fh:
                    try:
                        import fcntl  # noqa: PLC0415
                        fcntl.flock(fh, fcntl.LOCK_EX)
                    except ImportError:
                        pass                               # no inter-process lock on this platform
                    self._read_tail()                      # see other writers' events first, so seq is global
                    size = os.fstat(fh.fileno()).st_size
                    prefix = b""
                    if size > self._offset:                # a torn partial record from a crashed writer
                        os.ftruncate(fh.fileno(), self._offset)
                    elif size and not self._ends_with_newline():
                        prefix = b"\n"                     # a complete record that lacks its terminator
                    ev = Event(type=type, mission_id=mission_id, payload=to_jsonable(payload), seq=self._seq + 1)
                    ev.prev = self._heads.get(mission_id, "")
                    ev.hash = entry_hash(ev.prev, ev.seq, ev.ts, ev.type, ev.mission_id, ev.payload)
                    line = prefix + (json.dumps({"seq": ev.seq, "ts": ev.ts, "type": ev.type,
                                                 "mission_id": ev.mission_id, "payload": ev.payload,
                                                 "prev": ev.prev, "hash": ev.hash}) + "\n").encode("utf-8")
                    fh.write(line)                         # a failed write leaves memory untouched
                    fh.flush()
                    if _fsync_enabled():
                        os.fsync(fh.fileno())
                    self._offset += len(line)
                    self._seq = ev.seq
                    self._heads[mission_id] = ev.hash
                    self._events.append(ev)
            else:
                self._seq += 1
                ev = Event(type=type, mission_id=mission_id, payload=to_jsonable(payload), seq=self._seq)
                ev.prev = self._heads.get(mission_id, "")
                ev.hash = entry_hash(ev.prev, ev.seq, ev.ts, ev.type, ev.mission_id, ev.payload)
                self._heads[mission_id] = ev.hash
                self._events.append(ev)
        for cb in list(self._subscribers):
            try:
                cb(ev)
            except Exception:  # a subscriber must never break the log
                pass
        return ev

    def _ends_with_newline(self) -> bool:
        with open(self._path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self._subscribers.append(cb)

    def head(self, mission_id: str) -> str:
        """The chain head (hash of the mission's last event). Anchor it outside the log's writer's reach."""
        with self._lock:
            return self._heads.get(mission_id, "")

    def verify(self) -> ChainReport:
        with self._lock:
            return verify_chain(list(self._events))

    def for_mission(self, mission_id: str) -> list[Event]:
        return list(_mission_index(self).get(mission_id, ()))

    def all(self) -> list[Event]:
        return list(self._events)

    def mission_ids(self) -> list[str]:
        return [m for m in list(_mission_index(self)) if m]


# Durable store names: ``EventStore`` is the canonical durable (JSONL-backed) implementation; ``JsonlEventStore``
# is an alias so either name resolves to the same backend across the kernel and its overlays.
JsonlEventStore = EventStore


class MemoryEventStore:
    """In-memory EventStore with identical append/fold semantics to the durable store but no persistence sink —
    for fast, isolated replay in tests and ephemeral runs. Same event sequence → same folded state + order."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._seq = 0
        self._heads: dict[str, str] = {}
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[Event], None]] = []

    def head(self, mission_id: str) -> str:
        with self._lock:
            return self._heads.get(mission_id, "")

    def verify(self) -> ChainReport:
        with self._lock:
            return verify_chain(list(self._events))

    def append(self, type: str, mission_id: str, payload: dict[str, Any]) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(type=type, mission_id=mission_id, payload=to_jsonable(payload), seq=self._seq)
            ev.prev = self._heads.get(mission_id, "")
            ev.hash = entry_hash(ev.prev, ev.seq, ev.ts, ev.type, ev.mission_id, ev.payload)
            self._heads[mission_id] = ev.hash
            self._events.append(ev)
        for cb in list(self._subscribers):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001 - a subscriber must never break the log
                pass
        return ev

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self._subscribers.append(cb)

    def for_mission(self, mission_id: str) -> list[Event]:
        return list(_mission_index(self).get(mission_id, ()))

    def all(self) -> list[Event]:
        return list(self._events)

    def mission_ids(self) -> list[str]:
        return [m for m in list(_mission_index(self)) if m]


# Durable store names: ``EventStore`` is the canonical durable (JSONL-backed) implementation; ``JsonlEventStore``
# is an alias so either name resolves to the same backend across the kernel and its overlays.
JsonlEventStore = EventStore


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

    def observe(self, key: str, value: Any, source: str, confidence: float = 1.0,
                source_type: str = "prior", source_ref: str = "") -> None:
        self._store.append("ObservationWritten", self.mission_id,
                            {"key": key, "value": value, "source": source, "confidence": confidence,
                             "source_type": source_type, "source_ref": source_ref})

    def belief(self, key: str) -> Belief | None:
        obs = self._observations(key)
        if not obs:
            return None
        return self._fuse(obs, key)

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
            elif e.type == "GateExpired":
                # expiry is a notice-only staleness marker: the gate stays open, nothing is decided
                if e.payload.get("node_id") in parked:
                    parked[e.payload["node_id"]]["expired"] = True
                    parked[e.payload["node_id"]]["expired_at"] = e.payload.get("expired_at")
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
