"""Pure folds over a mission's persisted events. No I/O, no store handle, no clock: a list of events in, plain data out.

Everything here derives a displayed value from recorded fields with a FIXED rule (a template, a lookup table). Nothing is
inferred, summarised by a model, or looked up in live state, so the same events always produce the same answer and every
output can name the event it came from.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Optional

from . import summaries
from .models import MAX_PAYLOAD_BYTES, unavailable


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def event_sha256(e) -> str:
    """The cite of one persisted event: sha256 over its canonical body (seq, ts, type, mission, payload)."""
    return sha256_of({"seq": e.seq, "ts": e.ts, "type": e.type, "mission_id": e.mission_id, "payload": e.payload})


def cite(e) -> dict:
    return {"event_seq": e.seq, "event_sha256": event_sha256(e)}


# ───────────────────────── redaction ─────────────────────────
_SECRET_KEY = re.compile(r"(secret|token|password|passwd|api_?key|apikey|authorization|credentials?|private_?key|cookie)$", re.I)


def redact(value: Any, depth: int = 0) -> Any:
    """Replace the VALUE of any secret-looking key with an explicit ``redacted`` marker. Keys stay, so the shape is visible."""
    if depth > 12:
        return unavailable("too_large", "nesting deeper than 12 levels")
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY.search(k):
                out[k] = unavailable("redacted", "the value is withheld because the key names a secret")
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in value]
    return value


def payload_view(payload: Any):
    red = redact(payload)
    if len(canonical(red).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return unavailable("too_large", f"payload exceeds {MAX_PAYLOAD_BYTES} bytes; its cite still identifies it")
    return red


# ───────────────────────── classification (lookup tables, no inference) ─────────────────────────
_CATEGORY = {
    "lifecycle": ("MissionCreated", "MissionStateChanged", "MissionOutcome", "MissionBlocked", "MissionSuspended", "MissionResumed",
                  "MissionCancelled", "MissionQuarantined", "ReflectionEmitted", "RecommendationPromoted"),
    "plan": ("PlanCreated", "PlanActivated", "PlanSelected", "PlanReevaluated", "WaveScheduled", "BatchScheduled", "SchedulerConfigured"),
    "node": ("NodeDispatched", "NodeSucceeded", "NodeFailed", "NodeResumed", "NodeSkipped", "NodeCompensated", "NodeCompensationFailed"),
    "approval": ("NodeParked", "ApprovalGranted", "ApprovalRejected", "ApprovalInvalidated", "ApprovalNotHonoured", "ApprovalExpired",
                 "StandingGrantApplied", "StandingGrantDeclined"),
    "policy": ("PolicyEvaluated", "PolicyDenied", "PolicyChanged", "PolicyViolationDetected"),
    "verification": ("VerificationRecorded", "VerificationFailed", "VerificationOverridden", "VerificationRejected"),
    "world": ("ObservationWritten", "BeliefDisputed", "BeliefResolved"),
    "evidence": ("ClaimRaised", "EvidenceRecorded"),
    "replay": ("ReplayAuthorized", "ReplayRefused", "ReplayDivergence"),
}
CATEGORY_OF = {t: c for c, ts in _CATEGORY.items() for t in ts}

_FAILED = {"NodeFailed", "VerificationFailed", "MissionBlocked", "ReplayDivergence", "NodeCompensationFailed", "InferenceProviderFailed"}
_DENIED = {"PolicyDenied", "ReplayRefused", "ApprovalRejected", "VerificationRejected", "ApprovalNotHonoured", "StandingGrantDeclined"}
_PENDING = {"NodeParked", "ReconciliationRequired"}
_OK = {"NodeSucceeded", "ApprovalGranted", "NodeCompensated", "BeliefResolved", "ReplayAuthorized"}


def category_of(t: str) -> str:
    return CATEGORY_OF.get(t) or summaries.CATEGORY_OVERRIDES.get(t, "other")


def outcome_of(t: str, p: dict) -> str:
    if summaries.known(t):                              # the AOS-DD events and the operator-relevant base events (summaries.py)
        try:
            return summaries.outcome(t, p if isinstance(p, dict) else {})
        except Exception:                               # noqa: BLE001
            return "info"
    if t == "MissionOutcome":
        return "ok" if p.get("success") else "failed"
    if t == "VerificationRecorded":
        return {"accept": "ok", "reject": "failed", "escalate": "pending"}.get(str(p.get("decision")), "info")
    if t in _FAILED:
        return "failed"
    if t in _DENIED:
        return "denied"
    if t in _PENDING:
        return "pending"
    if t in _OK:
        return "ok"
    return "info"


_TEMPLATES = {
    "MissionCreated": "Mission created: {goal}",
    "MissionStateChanged": "State changed to {state}",
    "MissionOutcome": "Outcome recorded: {kind}",
    "MissionBlocked": "Mission blocked: {reason}",
    "PlanCreated": "Plan created (revision {revision}): {signature}",
    "PlanActivated": "Plan {plan_id} activated",
    "PlanSelected": "Plan selected from {considered} candidates",
    "NodeDispatched": "Step dispatched: {capability}",
    "NodeSucceeded": "Step succeeded: {capability}",
    "NodeFailed": "Step failed: {capability}: {error}",
    "NodeParked": "Step waiting for a human decision: {capability}",
    "NodeResumed": "Step resumed",
    "NodeSkipped": "Step skipped",
    "NodeCompensated": "Step compensated: {capability}",
    "ApprovalGranted": "Approval granted: {capability}",
    "ApprovalRejected": "Approval rejected",
    "ApprovalNotHonoured": "Approval not honoured: {reason}",
    "PolicyEvaluated": "Policy {effect}: {capability}",
    "PolicyDenied": "Policy denied: {capability}",
    "VerificationRecorded": "Verification {decision}",
    "VerificationFailed": "Verification failed: {capability}",
    "ObservationWritten": "World state observed: {key}",
    "ReplayAuthorized": "Replay authorized: {action}",
    "ReplayRefused": "Replay refused: {action}",
    "ReplayDivergence": "Replay diverged: {kind}",
    "ClaimRaised": "Claim raised",
    "EvidenceRecorded": "Evidence recorded",
}


class _Missing(dict):
    def __missing__(self, key):
        return ""


def summary_of(t: str, p: dict) -> str:
    """A fixed template over recorded fields. A field the event lacks is omitted, never replaced by a guess."""
    tpl = _TEMPLATES.get(t)
    if tpl is None:
        return summaries.summary(t, p) if summaries.known(t) else t
    flat = _Missing({k: v for k, v in p.items() if isinstance(v, (str, int, float))})
    text = tpl.format_map(flat)
    text = re.sub(r"\(revision \)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(\s*:\s*)+$", "", text)
    text = re.sub(r"\s+:", ":", text)
    return text[:240]


def actor_of(p: dict) -> dict:
    for key in ("actor", "approver", "principal"):
        v = p.get(key)
        if isinstance(v, dict):                         # a recorded principal (AuthenticatedPrincipal.record()): its id is the actor
            v = v.get("id")
        if isinstance(v, str) and v:
            return {"available": True, "value": v[:120]}
    return unavailable("not_recorded", "the event records no actor, approver or principal")


def mission_state(events: Iterable) -> Optional[str]:
    st = None
    for e in events:
        if e.type == "MissionCreated":
            st = "planning"
        elif e.type == "MissionStateChanged":
            st = str(e.payload.get("state")) if e.payload.get("state") is not None else st
    return st


def node_capabilities(events: Iterable) -> dict:
    """node_id -> capability, from the Node* and NodeParked events that name both."""
    out: dict = {}
    for e in events:
        nid, cap = e.payload.get("node_id"), e.payload.get("capability")
        if isinstance(nid, str) and isinstance(cap, str) and cap:
            out.setdefault(nid, cap)
    return out


def store_descriptor(store) -> dict:
    name = type(store).__name__
    if name == "EventStore":
        durability = "durable" if getattr(store, "_path", None) else "volatile"
    elif name == "DuckDBEventStore":
        durability = "volatile" if getattr(store, "_path", ":memory:") in (":memory:", "") else "durable"
    elif name == "PostgresEventStore":
        durability = "durable"
    else:
        durability = "unknown"
    return {"backend": name, "durability": durability}
