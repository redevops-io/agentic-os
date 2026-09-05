"""Project the Runtime's durable streams into canonical ``ControlEvidence`` (AGPL base).

Every mission-scoped stream folds into the one append-only ``EventStore`` (approvals, governance
grants, evidence/verification records are all typed ``Event`` rows), so most producers read
``events.for_mission(mission_id)`` and filter by ``Event.type``. The capability inventory and data
classifications come from the capability registry instead. All seams are duck-typed — a producer
needs only ``.for_mission(mission_id)`` on the event source and ``.all()`` on the registry — so it
composes with the real ``agentic_os.mission.store.EventStore`` / ``CapabilityRegistry`` or a fake.

Status discipline (mirrors the contract's vocabulary):
  * ``ENFORCED``  — the runtime *guarantees* the property structurally (the ledger is append-only;
    only registered capabilities can run). We emit this only for streams that are a guarantee, not a
    happens-to-have-occurred.
  * ``EVIDENCED`` — the runtime *holds evidence* the control was exercised (a human approved; a
    verification was recorded). Absence of such events yields no evidence at all — never a false
    ``ENFORCED`` — so a control with nothing to show stays ``UNVERIFIED`` downstream.

Nothing here fabricates evidence: a stream with no underlying records produces no rows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

from runtime_contracts import ControlEvidence, ControlStatus, RuntimeStream

# Event.type strings the runtime writes, grouped by the stream they evidence.
_APPROVAL_TYPES = ("ApprovalGranted", "ApprovalRejected", "NodeParked")
_GOVERNANCE_TYPES = ("PolicyChanged", "MissionSuspended", "MissionResumed",
                     "ApprovalInvalidated", "PolicyViolationDetected", "RecommendationPromoted")
_LINEAGE_TYPES = ("ClaimRaised", "EvidenceRecorded")
_VERIFICATION_TYPES = ("VerificationRecorded", "VerificationOverridden", "VerificationRejected")


def _iso(ts: Any) -> str:
    """Best-effort RFC3339 for an epoch float / already-a-string timestamp."""
    if isinstance(ts, str):
        return ts
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def _event_ref(ev: Any) -> str:
    """A stable per-event reference. Event has no id of its own, so key it by type#seq."""
    return f"{getattr(ev, 'type', 'event')}#{getattr(ev, 'seq', 0)}"


def _latest_ts(events: Sequence[Any]) -> str:
    ts = [getattr(e, "ts", None) for e in events if getattr(e, "ts", None) is not None]
    return _iso(max(ts)) if ts else ""


class RuntimeEvidenceProducer:
    """Projects durable runtime streams into ``ControlEvidence`` tagged with a ``RuntimeStream`` key.

    ``events`` is anything with ``for_mission(mission_id) -> Sequence[Event-like]`` (each Event-like
    has ``type``, ``seq``, ``ts``, ``payload``). ``registry`` (optional) is anything with
    ``all() -> Sequence[CapabilitySpec-like]`` (each has ``name``, ``operator``, and optionally
    ``data_classifications``). ``collector`` names who produced the evidence.
    """

    def __init__(self, *, events: Any, registry: Any = None, collector: str = "agentic-os") -> None:
        self._events = events
        self._registry = registry
        self._collector = collector

    # ---- mission-scoped streams (all read from the one append-only event ledger) ----

    def durable_event_ledger(self, mission_id: str) -> List[ControlEvidence]:
        """The mission event log is append-only and reload-on-restart durable → the immutable
        execution-log guarantee holds whenever the mission has recorded events."""
        evs = list(self._events.for_mission(mission_id))
        if not evs:
            return []
        return [self._ev(RuntimeStream.DURABLE_EVENT_LEDGER, mission_id, ControlStatus.ENFORCED,
                         events=evs)]

    def hitl_approval(self, mission_id: str) -> List[ControlEvidence]:
        """Evidence a human was in the loop: approvals granted/rejected and tasks parked for review."""
        return self._from_types(mission_id, RuntimeStream.HITL_APPROVAL, _APPROVAL_TYPES)

    def authority_grant(self, mission_id: str) -> List[ControlEvidence]:
        """Evidence of governance authority: policy changes, suspensions, invalidations."""
        return self._from_types(mission_id, RuntimeStream.AUTHORITY_GRANT, _GOVERNANCE_TYPES)

    def evidence_lineage(self, mission_id: str) -> List[ControlEvidence]:
        """Evidence-provenance records: claims raised and evidence recorded against them."""
        return self._from_types(mission_id, RuntimeStream.EVIDENCE_LINEAGE, _LINEAGE_TYPES)

    def verification_receipt(self, mission_id: str) -> List[ControlEvidence]:
        """Recorded verification outcomes (and human overrides/rejections of them)."""
        return self._from_types(mission_id, RuntimeStream.VERIFICATION_RECEIPT, _VERIFICATION_TYPES)

    # ---- system-scoped streams (from the capability inventory) ----

    def capability_manifest(self) -> List[ControlEvidence]:
        """The capability registry is the AI-system inventory; only registered capabilities run, so
        the identity+inventory property is a runtime guarantee whenever capabilities are registered."""
        specs = self._specs()
        if not specs:
            return []
        return [self._ev(RuntimeStream.CAPABILITY_MANIFEST, "runtime", ControlStatus.ENFORCED,
                         refs=tuple(f"capability:{getattr(s, 'name', '?')}" for s in specs))]

    def data_flow_classification(self) -> List[ControlEvidence]:
        """Data-flow classifications declared on capabilities. Evidence exists only for the
        capabilities that actually declare classifications — the rest stay unverified."""
        specs = [s for s in self._specs() if getattr(s, "data_classifications", None)]
        if not specs:
            return []
        return [self._ev(RuntimeStream.DATA_FLOW_CLASSIFICATION, "runtime", ControlStatus.EVIDENCED,
                         refs=tuple(f"capability:{getattr(s, 'name', '?')}" for s in specs))]

    # ---- aggregates ----

    def for_mission(self, mission_id: str) -> List[ControlEvidence]:
        """All mission-scoped evidence for one mission."""
        out: List[ControlEvidence] = []
        out += self.durable_event_ledger(mission_id)
        out += self.hitl_approval(mission_id)
        out += self.authority_grant(mission_id)
        out += self.evidence_lineage(mission_id)
        out += self.verification_receipt(mission_id)
        return out

    def system(self) -> List[ControlEvidence]:
        """All system-scoped evidence (inventory + data classifications)."""
        out: List[ControlEvidence] = []
        out += self.capability_manifest()
        out += self.data_flow_classification()
        return out

    def all(self, mission_ids: Optional[Iterable[str]] = None) -> List[ControlEvidence]:
        """Every stream for every mission (defaults to all missions the event source knows) plus
        the system-scoped streams."""
        if mission_ids is None:
            mission_ids = getattr(self._events, "mission_ids", lambda: [])()
        out: List[ControlEvidence] = []
        for mid in mission_ids:
            out += self.for_mission(mid)
        out += self.system()
        return out

    # ---- internals ----

    def _specs(self) -> List[Any]:
        if self._registry is None:
            return []
        return list(self._registry.all())

    def _from_types(self, mission_id: str, stream: RuntimeStream,
                    types: Sequence[str]) -> List[ControlEvidence]:
        matched = [e for e in self._events.for_mission(mission_id)
                   if getattr(e, "type", None) in types]
        if not matched:
            return []
        return [self._ev(stream, mission_id, ControlStatus.EVIDENCED, events=matched)]

    def _ev(self, stream: RuntimeStream, subject: str, status: ControlStatus, *,
            events: Sequence[Any] = (), refs: Sequence[str] = ()) -> ControlEvidence:
        return ControlEvidence(
            control_id=stream.value,
            subject=subject,
            status=status,
            mission_id=subject if events else "",
            event_ids=tuple(_event_ref(e) for e in events),
            artifact_refs=tuple(refs),
            collector=self._collector,
            observed_at=_latest_ts(events),
        )


def mission_evidence(events: Any, mission_id: str, *, registry: Any = None,
                     collector: str = "agentic-os") -> List[ControlEvidence]:
    """Convenience: all mission-scoped evidence for one mission."""
    return RuntimeEvidenceProducer(events=events, registry=registry,
                                   collector=collector).for_mission(mission_id)


def system_evidence(registry: Any, *, collector: str = "agentic-os") -> List[ControlEvidence]:
    """Convenience: system-scoped evidence (capability inventory + data classifications)."""
    return RuntimeEvidenceProducer(events=_NO_EVENTS, registry=registry,
                                   collector=collector).system()


class _NoEvents:
    def for_mission(self, mission_id: str) -> List[Any]:
        return []

    def mission_ids(self) -> List[str]:
        return []


_NO_EVENTS = _NoEvents()
