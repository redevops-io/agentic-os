"""AI/agent security — the ReDevOps runtime as an Edge Sentinel datasource (Phase 9).

The plan: instrument the agentic runtime's own events as SecurityObservations, so an induced unsafe
agent/tool sequence opens a *normal* investigation and **cannot bypass Governance**. This dogfoods Edge
Sentinel on ReDevOps itself.

The boundary is the platform's OWN generic envelope — ``agentic_os.mission.events.RuntimeEvent``
(runtime-event/v10), which is already the single forensic timeline every subsystem (Context / Mission /
Discovery Runtimes, Sidekick, external-agent adapters) emits. Edge Sentinel consumes that contract rather
than a hand-rolled ``RuntimeSecurityEvent`` replica — the security projection is a *view* over the real
event, so if the runtime-event schema moves, this breaks loudly instead of drifting.

Crucially, Edge Sentinel here is an OBSERVER. It turns runtime events into evidence + findings and opens an
AI_SECURITY case; any response goes through the same governed path as everything else (Phase 7). A
``POLICY_DECISION = DENIED`` in the timeline is the runtime's Governance holding — Edge Sentinel records the
*attempt*, it does not (and cannot) re-drive the denied action.
"""
from __future__ import annotations

from agentic_os.mission.events import EventType, ResultStatus, RuntimeEvent

from .evidence import (
    CaseStatus,
    CaseType,
    EvidenceArtifact,
    Finding,
    FindingStatus,
    SecurityCase,
    SecurityObservation,
    Severity,
    _now,
    canonical_json,
)

# Substring markers of prompt-injection / instruction-override attempts (whole-payload scan; deterministic).
_INJECTION_MARKERS = (
    "ignore previous", "ignore all previous", "disregard", "you are now", "system prompt",
    "reveal your", "exfiltrate", "override", "developer mode", "do anything now", "bypass",
    "print your instructions", "leak", "sudo make me",
)


def security_projection(evt: RuntimeEvent) -> dict:
    """The security-relevant view of a runtime event (a projection, not a copy of the whole envelope)."""
    return {
        "actor": evt.actor,
        "event_type": evt.event_type.value,
        "capability_id": evt.capability_id,
        "result_status": evt.result_status.value,
        "mission_id": evt.mission_id,
        "source_runtime": evt.source_runtime or "runtime",
        "policy_context": evt.policy_context,
    }


def _has_injection(payload: dict) -> bool:
    blob = canonical_json(payload).lower()
    return any(m in blob for m in _INJECTION_MARKERS)


def observation_from_runtime_event(store, evt: RuntimeEvent) -> SecurityObservation:
    """Instrument one runtime event as an immutable-evidence-backed SecurityObservation."""
    raw = evt.to_ndjson()          # the runtime-event/v10 canonical serialization (sorted-key JSON string)
    ev = store.put_evidence(
        EvidenceArtifact.of_raw("runtime_event", evt.source_runtime or "runtime", raw,
                                observed_at=str(evt.timestamp)), raw)
    return store.put_observation(SecurityObservation(
        source=evt.source_runtime or "runtime", source_type="runtime",
        observed_at=str(evt.timestamp), known_at=ev.known_at, raw_evidence_ref=ev.artifact_id,
        normalized_fields=security_projection(evt),
        identity_refs=(f"actor:{evt.actor}",) if evt.actor else ()))


def ingest_runtime_events(store, events: list[RuntimeEvent]):
    """Return [(event, observation)] — each event instrumented as evidence-backed observation."""
    return [(e, observation_from_runtime_event(store, e)) for e in events]


def assess_ai_security(pairs) -> list[Finding]:
    """Deterministic detection over the instrumented event sequence. No model in the loop.

    Detects: (1) prompt-injection markers in a proposal/invocation; (2) an action ATTEMPTED after a
    POLICY_DECISION denied it — a governance-bypass attempt that governance HELD; (3) a capability that
    was itself DENIED. Each finding links to the triggering event's immutable evidence."""
    findings: list[Finding] = []
    # (actor, capability) pairs that governance denied
    denied_caps = {(e.actor, e.capability_id) for e, _ in pairs
                   if e.event_type is EventType.POLICY_DECISION and e.result_status is ResultStatus.DENIED}

    for evt, obs in pairs:
        ev_ref = obs.raw_evidence_ref
        if evt.event_type in (EventType.TOOL_PROPOSAL, EventType.CAPABILITY_INVOCATION) and _has_injection(evt.payload):
            findings.append(Finding(
                claim=f"Prompt-injection markers in {evt.actor}'s {evt.event_type.value} "
                      f"({evt.capability_id or 'no capability'})",
                confidence=0.7, evidence_refs=(ev_ref,), attack_refs=("T1059",),
                affected_assets=(f"actor:{evt.actor}",),
                recommended_actions=(), status=FindingStatus.SUPPORTED))
        if (evt.event_type is EventType.CAPABILITY_INVOCATION
                and evt.result_status in (ResultStatus.ATTEMPTED, ResultStatus.PROPOSED)
                and (evt.actor, evt.capability_id) in denied_caps):
            findings.append(Finding(
                claim=f"{evt.actor} attempted '{evt.capability_id}' after policy DENIED it — "
                      f"Governance held; recording the bypass attempt",
                confidence=0.9, evidence_refs=(ev_ref,), affected_assets=(f"actor:{evt.actor}",),
                status=FindingStatus.SUPPORTED))
        if evt.result_status is ResultStatus.DENIED and evt.event_type in (
                EventType.CAPABILITY_INVOCATION, EventType.TOOL_RESULT):
            findings.append(Finding(
                claim=f"{evt.actor}'s '{evt.capability_id}' was DENIED by Governance",
                confidence=1.0, evidence_refs=(ev_ref,), affected_assets=(f"actor:{evt.actor}",),
                status=FindingStatus.CONFIRMED))
    return findings


def open_ai_security_case(store, events: list[RuntimeEvent], *, title: str = "",
                          severity: Severity = Severity.HIGH) -> SecurityCase:
    """Open a NORMAL investigation (AI_SECURITY case) from a runtime event sequence: instrument events as
    evidence-backed observations, assess for unsafe patterns, and link everything. No action is taken here
    — a response, if any, goes through the Phase-7 governed path. Returns the case (with no findings the
    case simply reflects a clean sequence)."""
    pairs = ingest_runtime_events(store, events)
    findings = assess_ai_security(pairs)
    for f in findings:
        store.put_finding(f)
    obs_refs = tuple(o.id for _, o in pairs)
    ev_refs = tuple(dict.fromkeys(o.raw_evidence_ref for _, o in pairs))
    case = SecurityCase(
        case_type=CaseType.AI_SECURITY, severity=severity, created_at=_now(), known_at=_now(),
        title=title or "AI/agent security investigation",
        status=CaseStatus.INVESTIGATING if findings else CaseStatus.OPEN,
        observation_refs=obs_refs, evidence_refs=ev_refs,
        finding_refs=tuple(f.finding_id for f in findings))
    return store.put_case(case)
