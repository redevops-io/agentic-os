"""The sentinel feed: one canonical stream of the events a security monitor (Edge Sentinel) must see, from every part of the autonomy stack.

The goal-loop controller, the child-agent spawner, the reconciliation supervisor, context compaction, the egress proxy and the assurance
sensors each report in their own shape (`on_event` callbacks, a JSON decision log, findings). A monitor that has to learn six formats
will miss one. This module normalizes them into `SentinelEvent` and appends them to a durable, append-only feed the monitor PULLS from.

Properties:
  * One shape: kind, severity, tenant, subject (loop / mission / child), source component, a human line, structured evidence, a time.
  * Idempotent: every event has a stable dedupe key built from what happened (not when), so a retried callback, a replayed log line or a
    restart does not raise the same alert twice. Repeats of a condition that is still true are recorded as one event.
  * Durable and read-only for the consumer: events live in an event store under their own scope; `pull(cursor)` returns what is new and a new
    cursor, so a restart of the monitor resumes where it stopped and never loses an event.
  * A sink never breaks what it observes: adapters swallow their own errors.
  * An alert is not closure: nothing here closes, resolves or acts on anything.

What it does not do: it does not decide what to do about an event (that is the monitor's case workflow), it does not deliver pages, and the
egress adapter reads a decision log the proxy already wrote; it does not inspect payloads.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

SCOPE = "__sentinel__"
SEVERITIES = ("info", "low", "medium", "high", "critical")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# what each source kind means for a monitor
KINDS = {
    # goal-loop controller
    "LOOP_STALLED": ("medium", "a goal loop is making no progress and is being replanned"),
    "BUDGET_NEAR_LIMIT": ("low", "a goal loop is near one of its hard limits"),
    "LOOP_ESCALATED": ("high", "a goal loop could not find a way forward and needs a person"),
    "LOOP_LIMIT_REACHED": ("high", "a goal loop hit a hard limit and stopped"),
    "LOOP_COMPLETED": ("info", "a goal loop finished"),
    "LOOP_CANCELLED": ("info", "a goal loop was cancelled"),
    # child agents
    "CHILD_SPAWNED": ("info", "a child agent was created"),
    "CHILD_REFUSED": ("medium", "a child agent was refused (outside its parent's authority or allowance)"),
    "CHILD_CLOSED": ("info", "a child agent ended"),
    "CHILD_REVOKED": ("high", "a child agent was revoked"),
    # reconciliation supervisor
    "UNKNOWN_OUTCOME": ("high", "an action's outcome is unknown and could not be resolved automatically"),
    "RECONCILE_RESOLVED": ("info", "an unknown outcome was resolved with evidence"),
    # context integrity
    "CONTEXT_COMPACTED": ("info", "an agent's context was compacted"),
    "CONTEXT_COMPACTION_FAILED": ("low", "a context compaction was refused and the history was kept"),
    "SCOPE_REINJECTED": ("high", "an agent's pinned scope was missing or altered and was restored"),
    "SCOPE_TAMPERED": ("critical", "a message in an agent's context claimed to be the owner's pinned scope"),
    # egress
    "EGRESS_DENIED": ("medium", "an outbound connection was refused by the egress policy"),
    "EGRESS_BLOCKED_ADDRESS": ("high", "an outbound connection to a metadata or internal address was refused"),
}
_ASSURANCE_SEVERITY = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}


@dataclass(frozen=True)
class SentinelEvent:
    kind: str
    severity: str
    tenant: str
    subject: str                      # loop id / mission id / child id / client address: what the event is about
    source: str                       # goal_loop | children | reconcile | context | egress | assurance
    detail: str
    evidence: dict = field(default_factory=dict)
    ts: float = 0.0
    key: str = ""                     # stable dedupe key

    def to_dict(self) -> dict:
        return {"kind": self.kind, "severity": self.severity, "tenant": self.tenant, "subject": self.subject, "source": self.source,
                "detail": self.detail, "evidence": self.evidence, "ts": self.ts, "key": self.key}


def _key(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def severity_at_least(sev: str, floor: str) -> bool:
    return _RANK.get(sev, 0) >= _RANK.get(floor, 0)


class SentinelFeed:
    """`SentinelFeed(store)`: `store` is an event store (the same kind the runtime uses). Hand the `*_sink` methods to the components as their
    `on_event` and call `pull()` from the monitor."""

    def __init__(self, store, *, scope: str = SCOPE, clock: Callable[[], float] = time.time, repeat_after_s: float = 3600.0) -> None:
        self.store, self.scope, self.clock, self.repeat_after_s = store, scope, clock, float(repeat_after_s)

    # ── writing ──
    def emit(self, ev: SentinelEvent) -> bool:
        """Append unless the same thing was already reported within `repeat_after_s`. True when it was recorded."""
        try:
            now = self.clock()
            ev = SentinelEvent(**{**ev.__dict__, "ts": ev.ts or now})
            for e in reversed(self.store.for_mission(self.scope)):
                if e.type != "SentinelEvent":
                    continue
                if e.payload.get("key") == ev.key and now - float(e.payload.get("ts", 0.0)) < self.repeat_after_s:
                    return False
            self.store.append("SentinelEvent", self.scope, ev.to_dict())
            return True
        except Exception:  # noqa: BLE001 - a sentinel sink must never break the component it observes
            return False

    # ── adapters: each takes the shape its component already produces ──
    def _from_kind(self, d: dict, source: str, subject: str, key_extra: Any = "") -> Optional[SentinelEvent]:
        kind = str(d.get("type", ""))
        if kind not in KINDS:
            return None
        sev, line = KINDS[kind]
        tenant = str(d.get("tenant", ""))
        ev_fields = {k: v for k, v in d.items() if k not in ("type", "ts", "tenant")}
        return SentinelEvent(kind, sev, tenant, subject, source, f"{line}", ev_fields, float(d.get("ts", 0.0) or 0.0),
                             _key(kind, subject, key_extra))

    def loop_sink(self, d: dict) -> None:
        """For `GoalLoop(on_event=...)`."""
        key = d.get("kind") or d.get("cap") or d.get("limit") or ""
        ev = self._from_kind(d, "goal_loop", str(d.get("loop_id", "")), key)
        if ev:
            self.emit(ev)

    def child_sink(self, d: dict) -> None:
        """For `ChildSpawner(on_event=...)`."""
        subject = str(d.get("child_id") or d.get("parent") or "")
        ev = self._from_kind(d, "children", subject, d.get("code") or d.get("role") or "")
        if ev:
            self.emit(ev)

    def reconcile_sink(self, d: dict) -> None:
        """For `ReconciliationSupervisor(on_event=...)`."""
        ev = self._from_kind(d, "reconcile", str(d.get("mission_id", "")), d.get("job_id", ""))
        if ev:
            self.emit(ev)

    def context_sink(self, d: dict) -> None:
        """For `compact_with_integrity(on_event=...)`."""
        ev = self._from_kind(d, "context", str(d.get("mission_id", "")), d.get("digest") or d.get("summary_digest") or d.get("reason") or "")
        if ev:
            self.emit(ev)

    def egress(self, record: "dict | str") -> Optional[SentinelEvent]:
        """One line of the egress proxy's decision log. Only refusals are events; allowed traffic is not."""
        try:
            r = json.loads(record) if isinstance(record, str) else dict(record)
        except Exception:  # noqa: BLE001
            return None
        if r.get("event") or r.get("allow", True):
            return None
        rule = str(r.get("rule", ""))
        reason = str(r.get("reason", ""))
        blocked_addr = any(w in (rule + " " + reason).lower() for w in ("metadata", "ssrf", "private", "loopback", "link-local"))
        kind = "EGRESS_BLOCKED_ADDRESS" if blocked_addr else "EGRESS_DENIED"
        sev, line = KINDS[kind]
        host = f"{r.get('host', '')}:{r.get('port', '')}"
        ev = SentinelEvent(kind, sev, str(r.get("tenant", "")), str(r.get("client", "")), "egress", f"{line}: {host}",
                           {"host": r.get("host"), "port": r.get("port"), "rule": rule, "reason": reason[:200], "identity": r.get("identity")},
                           float(r.get("ts", 0.0) or 0.0), _key(kind, r.get("client"), host, rule))
        self.emit(ev)
        return ev

    def assurance(self, findings: Iterable) -> int:
        """`assurance.assurance_findings(...)` output: each finding becomes one event, keyed by what it points at."""
        n = 0
        for f in findings:
            sev = _ASSURANCE_SEVERITY.get(getattr(f, "severity", "medium"), "medium")
            ev = SentinelEvent(f"ASSURANCE_{str(f.kind).upper()}", sev, "", f.mission_id, "assurance", f.detail,
                               {"node_id": f.node_id, "seq": f.seq}, 0.0, _key("assurance", f.kind, f.mission_id, f.node_id, f.seq))
            n += 1 if self.emit(ev) else 0
        return n

    # ── reading ──
    def pull(self, cursor: int = 0, *, min_severity: str = "info", limit: int = 1000) -> tuple:
        """`(events, next_cursor)`: what was recorded after `cursor` at or above `min_severity`. The monitor stores `next_cursor` durably."""
        evs = [e for e in self.store.for_mission(self.scope) if e.type == "SentinelEvent"]
        out = []
        for i in range(cursor, len(evs)):
            p = evs[i].payload
            if severity_at_least(p.get("severity", "info"), min_severity):
                out.append(dict(p))
            if len(out) >= limit:
                return out, i + 1
        return out, len(evs)
