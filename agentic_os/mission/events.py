"""Normalized runtime events — the public event vocabulary + local, replayable event store.

The load-bearing idea (from the cross-agent-observability model): **every supported agent surface, live
or persisted, normalizes into the same typed, replayable event history.** Python is the executable
specification; this mirrors the v9 amendment's canonical event schemas and the v10 ``RuntimeEvent`` family.

This module is the public contract:

* one versioned :class:`RuntimeEvent` envelope (content-addressed id → deterministic, replayable) +
  thin specialization constructors;
* the versioned per-record :data:`SCHEMAS` and :meth:`RuntimeEvent.validate`;
* an append-only, physically-ordered :class:`EventLedger` with causal edges + NDJSON serialization —
  a minimal *local* event store, enough to record, replay, audit and conform.

What is NOT here (enterprise overlay): fleet-wide **enforcement** (the clean-decision Decision Gate),
**policy correlation** (the Trajectory rule engine), enterprise event ingestion, forensic storage,
cross-tenant analytics, and operational console aggregation. Those consume this same vocabulary; a
public-only deployment records and replays events without them.

Contract version: :data:`SCHEMA_VERSION` (``runtime-event/v1``). Additive-only within a major (new event
types, new optional envelope fields, new specialized schemas); a breaking change to the envelope or the
NDJSON serialization bumps the major. The ``_demo`` self-test is the executable golden fixture.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from hashlib import sha256


# ════════════════════════════ Runtime Event Contract ════════════════════════════
class EventType(str, Enum):
    DEREFERENCE = "dereference"; CAPABILITY_INVOCATION = "capability_invocation"
    TOOL_PROPOSAL = "tool_proposal"; TOOL_RESULT = "tool_result"; VERIFICATION = "verification"
    MERGE = "merge"; PROMOTION = "promotion"; POLICY_DECISION = "policy_decision"
    HUMAN_REVIEW = "human_review"


class ResultStatus(str, Enum):
    PROPOSED = "proposed"; ATTEMPTED = "attempted"; COMPLETED = "completed"
    FAILED = "failed"; OBSERVED = "observed"; DENIED = "denied"


class Severity(str, Enum):
    INFORMATIONAL = "informational"; LOW = "low"; MEDIUM = "medium"; HIGH = "high"; CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["informational", "low", "medium", "high", "critical"].index(self.value)


#: Semantic version of the public runtime-event contract (the envelope + NDJSON serialization).
SCHEMA_VERSION = "runtime-event/v1"
CONTRACT_VERSION = SCHEMA_VERSION

# the versioned schemas the v9 amendment enumerates (required fields per record kind)
SCHEMAS: dict[str, list[str]] = {
    "runtime-event/v1": ["event_id", "event_type", "schema_version", "timestamp", "actor", "result_status"],
    "dereference-event/v1": ["event_id", "artifact_handles", "visibility"],
    "capability-event/v1": ["event_id", "capability_id"],
    "verification-event/v1": ["event_id", "result_status"],
    "policy-decision/v1": ["event_id", "policy_context"],
    "session-summary/v1": ["session_id", "event_count"],
}


@dataclass
class RuntimeEvent:
    """The one normalized envelope every subsystem emits — Context/Mission/Discovery Runtimes,
    Sidekick, and external-agent adapters share this single forensic timeline."""
    event_type: EventType
    actor: str
    timestamp: int                                  # caller-supplied logical time (deterministic/replayable)
    schema_version: str = SCHEMA_VERSION
    mission_id: str = ""
    session_id: str = ""
    parent_event_id: str = ""
    capability_id: str = ""
    artifact_handles: list[str] = field(default_factory=list)
    source_runtime: str = ""
    source_agent: str = ""
    visibility: str = "private"
    policy_context: str = ""
    result_status: ResultStatus = ResultStatus.OBSERVED
    evidence_refs: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:                       # content-addressed id → deterministic, replayable
            key = f"{self.event_type.value}|{self.actor}|{self.timestamp}|{self.mission_id}|{self.parent_event_id}|{sorted(self.artifact_handles)}|{json.dumps(self.payload, sort_keys=True)}"
            self.event_id = "ev-" + sha256(key.encode()).hexdigest()[:16]

    def to_ndjson(self) -> str:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        d["result_status"] = self.result_status.value
        return json.dumps(d, sort_keys=True)

    @classmethod
    def from_ndjson(cls, line: str) -> "RuntimeEvent":
        d = json.loads(line)
        d["event_type"] = EventType(d["event_type"])
        d["result_status"] = ResultStatus(d["result_status"])
        return cls(**d)

    def validate(self) -> list[str]:
        """Missing required fields per the envelope + the type's specialized schema (empty = valid)."""
        spec_schema = {
            EventType.DEREFERENCE: "dereference-event/v1", EventType.CAPABILITY_INVOCATION: "capability-event/v1",
            EventType.VERIFICATION: "verification-event/v1", EventType.POLICY_DECISION: "policy-decision/v1",
        }.get(self.event_type)
        d = asdict(self)
        missing = [f for f in SCHEMAS["runtime-event/v1"] if not d.get(f)]
        if spec_schema:
            missing += [f for f in SCHEMAS[spec_schema] if not d.get(f)]
        return missing


# specialization constructors (thin — the envelope is the contract)
def dereference_event(actor, timestamp, artifact_handles, visibility="private", **kw) -> RuntimeEvent:
    return RuntimeEvent(EventType.DEREFERENCE, actor, timestamp, artifact_handles=artifact_handles, visibility=visibility, **kw)


def capability_event(actor, timestamp, capability_id, result_status=ResultStatus.ATTEMPTED, **kw) -> RuntimeEvent:
    return RuntimeEvent(EventType.CAPABILITY_INVOCATION, actor, timestamp, capability_id=capability_id, result_status=result_status, **kw)


def tool_result_event(actor, timestamp, capability_id, result_status=ResultStatus.COMPLETED, **kw) -> RuntimeEvent:
    return RuntimeEvent(EventType.TOOL_RESULT, actor, timestamp, capability_id=capability_id, result_status=result_status, **kw)


def policy_decision_event(actor, timestamp, policy_context, result_status, **kw) -> RuntimeEvent:
    return RuntimeEvent(EventType.POLICY_DECISION, actor, timestamp, policy_context=policy_context, result_status=result_status, **kw)


class EventLedger:
    """Append-only, physically-ordered event history with causal (parent) edges. Never mutates or
    erases — a model rewind, an abandoned branch, and a superseded proposal are all retained. This is the
    minimal *local* store; enterprise ingestion/forensics/analytics consume the same NDJSON."""

    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []
        self._by_id: dict[str, RuntimeEvent] = {}

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        self._events.append(event)                  # physical order preserved; never rewritten
        self._by_id[event.event_id] = event
        return event

    def events(self) -> list[RuntimeEvent]:
        return list(self._events)

    def get(self, event_id: str) -> RuntimeEvent | None:
        return self._by_id.get(event_id)

    def children(self, event_id: str) -> list[RuntimeEvent]:
        return [e for e in self._events if e.parent_event_id == event_id]

    def by_session(self, session_id: str) -> list[RuntimeEvent]:
        return [e for e in self._events if e.session_id == session_id]

    def to_ndjson(self) -> str:
        return "\n".join(e.to_ndjson() for e in self._events)

    @classmethod
    def from_ndjson(cls, text: str) -> "EventLedger":
        led = cls()
        for line in text.splitlines():
            if line.strip():
                led.append(RuntimeEvent.from_ndjson(line))
        return led

    def session_summary(self, session_id: str) -> dict:
        evs = self.by_session(session_id)
        return {"schema_version": "session-summary/v1", "session_id": session_id, "event_count": len(evs),
                "types": sorted({e.event_type.value for e in evs}),
                "failed": sum(1 for e in evs if e.result_status is ResultStatus.FAILED)}


# ════════════════════════════ self-test / golden fixture ════════════════════════════
def _demo() -> int:
    checks: list[tuple[str, bool]] = []

    # content-addressed id is deterministic (same inputs → same id → replayable)
    e1 = capability_event("agent", 1, "cap.read", mission_id="m1")
    e1b = capability_event("agent", 1, "cap.read", mission_id="m1")
    checks.append(("content-addressed event_id is deterministic", e1.event_id == e1b.event_id))

    # NDJSON round-trips byte-stably
    checks.append(("RuntimeEvent NDJSON round-trips", RuntimeEvent.from_ndjson(e1.to_ndjson()).to_ndjson() == e1.to_ndjson()))

    # validate flags a missing required field on a specialized schema
    bad = RuntimeEvent(EventType.DEREFERENCE, "agent", 2)   # no artifact_handles
    checks.append(("validate catches a missing specialized field", "artifact_handles" in bad.validate()))
    good = dereference_event("agent", 3, ["art-1"])
    checks.append(("a complete event validates clean", good.validate() == []))

    # ledger preserves physical order + causal edges, and replays from NDJSON
    led = EventLedger()
    p = led.append(capability_event("agent", 4, "cap.plan", session_id="s1"))
    led.append(tool_result_event("agent", 5, "cap.plan", session_id="s1", parent_event_id=p.event_id))
    replay = EventLedger.from_ndjson(led.to_ndjson())
    checks.append(("ledger replays from NDJSON identically", replay.to_ndjson() == led.to_ndjson()))
    checks.append(("causal children resolve", len(led.children(p.event_id)) == 1))
    checks.append(("session summary counts + types", led.session_summary("s1")["event_count"] == 2))

    for name, ok in checks:
        print(f"  check {name} = {ok}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"RESULT {passed}/{len(checks)}  (event contract {SCHEMA_VERSION})")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_demo())
