"""Agent-runtime events as Edge Sentinel cases: the consuming end of the sentinel feed (`agentic_os.mission.sentinel`).

The mission kernel, the goal-loop controller, child agents, reconciliation, context integrity and the egress proxy all report what an
autonomous agent is doing. This turns those events into the same evidence -> observation -> finding -> case spine a CrowdSec alert uses
(`incident.py`), so an analyst sees "a goal loop is stuck", "a child agent was revoked" or "an agent's pinned scope was tampered with" next to
the network alerts, with the raw event kept as immutable, content-addressed evidence.

Rules:
  * An event is a SIGNAL, never a closure and never an action: the case is INVESTIGATING, the finding recommends no action, and nothing here
    blocks, revokes or retries anything. Closing needs a verified terminal state elsewhere.
  * Idempotent: the same event yields the same content-addressed ids, so a re-pulled event or a restarted consumer does not duplicate a case.
  * Only events at or above `min_severity` open a case (default medium); the rest stay in the feed.
  * `pull_cases` keeps the feed cursor with the store's caller, so the consumer resumes where it stopped.
"""
from __future__ import annotations

from .evidence import (
    CaseStatus, CaseType, EvidenceArtifact, Finding, FindingStatus, SecurityCase, SecurityObservation, Severity, canonical_json)

_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def ingest_agent_event(store, event: dict, *, min_severity: str = "medium"):
    """The ingestion path for one sentinel event dict. Returns the case, or None when the event is below `min_severity`."""
    sev = str(event.get("severity", "info"))
    if _RANK.get(sev, 0) < _RANK.get(min_severity, 2):
        return None
    raw = canonical_json(event)
    ev = store.put_evidence(EvidenceArtifact.of_raw("agent_event", "agentic-os", raw, observed_at=_iso(event.get("ts"))), raw)
    obs = store.put_observation(SecurityObservation(
        source="agentic-os", source_type="runtime", observed_at=_iso(event.get("ts")) or ev.acquired_at, known_at=ev.known_at,
        raw_evidence_ref=ev.artifact_id,
        normalized_fields={"kind": event.get("kind"), "severity": sev, "tenant": event.get("tenant", ""), "subject": event.get("subject", ""),
                           "component": event.get("source", ""), "detail": event.get("detail", "")},
        identity_refs=(f"subject:{event.get('subject')}",) if event.get("subject") else (), confidence=1.0))
    finding = store.put_finding(Finding(
        claim=f"{event.get('kind')} on {event.get('subject') or 'unknown'} ({event.get('source')}): {event.get('detail')}",
        confidence=1.0, evidence_refs=(ev.artifact_id,), recommended_actions=(), status=FindingStatus.SUPPORTED))
    case = SecurityCase(
        case_type=CaseType.AI_SECURITY, severity=Severity(sev), created_at=obs.observed_at or ev.known_at, known_at=ev.known_at,
        title=f"Agent runtime: {event.get('kind')} ({event.get('subject') or 'unknown'})", status=CaseStatus.INVESTIGATING,
        observation_refs=(obs.id,), evidence_refs=(ev.artifact_id,), finding_refs=(finding.finding_id,))
    return store.cases.get(case.case_id) or store.put_case(case)


def pull_cases(store, feed, cursor: int = 0, *, min_severity: str = "medium") -> tuple:
    """Pull new events from a `SentinelFeed` and open a case for each. Returns `(cases, next_cursor)`; keep `next_cursor` durably."""
    events, nxt = feed.pull(cursor, min_severity=min_severity)
    cases = [c for c in (ingest_agent_event(store, e, min_severity=min_severity) for e in events) if c is not None]
    return cases, nxt


def _iso(ts) -> str:
    if not ts:
        return ""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
