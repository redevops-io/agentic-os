"""Collectors: turn what other parts of the stack already produce into signed evidence envelopes.

A collector is a PRODUCER: it signs with its own registered key, states the window its data covers, and states whether the population it reports is complete.
It never decides a result; the assessor reads the data, not the collector's opinion of it.
"""
from __future__ import annotations

import json
import secrets
from typing import Iterable, Optional

from .vault import EvidenceEnvelope, sign_envelope


def build_envelope(kind: str, payload: dict, *, tenant: str, scope: str, source: str, producer: str, key_id: str, secret: bytes, observed_at: float,
                   coverage_from: float, coverage_to: float, nonce: Optional[str] = None, classification: str = "restricted", meta: Optional[dict] = None
                   ) -> EvidenceEnvelope:
    env = EvidenceEnvelope(tenant=tenant, scope=scope, kind=kind, source=source, producer=producer, key_id=key_id,
                           payload=json.dumps(payload, sort_keys=True, default=str).encode(), observed_at=observed_at, coverage_from=coverage_from,
                           coverage_to=coverage_to, nonce=nonce or secrets.token_hex(12), classification=classification, meta=meta or {})
    return sign_envelope(env, secret)


def from_sentinel_events(events: Iterable[dict], **kw) -> EvidenceEnvelope:
    """The agent-runtime events the sentinel feed carries (unknown outcomes, refused attempts, revocations) as one evidence artifact for a window."""
    evs = list(events)
    return build_envelope("agent_runtime_events", {"events": evs, "population_complete": True, "count": len(evs)}, source="agentic-os sentinel feed", **kw)


def reconciliation_evidence(outcomes: Iterable[dict], *, population_complete: bool, **kw) -> EvidenceEnvelope:
    """Outcomes of side-effecting actions: [{id, state: 'resolved'|'unknown', opened_at, resolved_at}]. An unknown outcome is an exception to be worked, not a pass or a fail
    of the action itself."""
    return build_envelope("reconciliation", {"outcomes": list(outcomes), "population_complete": population_complete}, source="agentic-os reconciliation", **kw)
