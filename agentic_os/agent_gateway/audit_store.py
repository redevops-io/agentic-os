"""A durable audit sink for the Agent Gateway, backed by the kernel's own append-only event log.

``InMemoryAuditSink`` (the default) loses every record when the process exits. :class:`EventStoreAuditSink`
writes each :class:`AuditEvent` as one ``agent_gateway.call`` event to an :class:`agentic_os.mission.store.EventStore`
(a JSONL file, fsync'd per append, hash-chained so an edited, removed or reordered record is detected by
``verify_chain``). It adds no dependency: the store is the one the missions already use.

Nothing is stored beyond what ``AuditEvent.as_record()`` already carries (who, which capability, the decision,
latency, evidence references); capability arguments and outputs are never part of an audit event.

The store keeps the records it has read in memory as well as on disk, so a long-lived process that serves a
large number of calls grows with them; rotate the file by restarting with a new path.
"""
from __future__ import annotations

from typing import Any

from .contracts import AuditEvent

AUDIT_EVENT_TYPE = "agent_gateway.call"
AUDIT_STREAM_ID = "agent-gateway"       # one hash chain for the whole gateway audit log


class EventStoreAuditSink:
    """Implements :class:`agentic_os.agent_gateway.AuditSink` over any ``EventStoreProtocol``."""

    def __init__(self, store: Any, stream_id: str = AUDIT_STREAM_ID) -> None:
        self._store = store
        self._stream_id = stream_id

    @classmethod
    def from_path(cls, path: str) -> "EventStoreAuditSink":
        """Open (or create) the JSONL audit log at ``path``. Raises ``OSError`` if it cannot be written, so a
        misconfigured path fails at startup instead of silently auditing nowhere."""
        from ..mission.store import EventStore  # noqa: PLC0415 - keep the import out of the gateway's import path

        store = EventStore(path)
        # Prove the path is writable now (an unwritable path must stop startup, not the first request).
        with open(path, "ab"):
            pass
        return cls(store)

    def record(self, event: AuditEvent) -> str:
        ev = self._store.append(AUDIT_EVENT_TYPE, self._stream_id, event.as_record())
        return f"audit_{ev.seq}"

    def events(self) -> list:
        return list(self._store.for_mission(self._stream_id))
