"""Append-only, content-addressed store for the Edge Sentinel evidence spine (Phase 1).

Deliberately NOT a database — an in-memory, append-only store that is enough to prove the invariant the
plan cares about: raw evidence is immutable and content-addressed, and everything else references it, so a
case is *replayable* from its evidence. A durable backend (the Mission Event Store) slots in behind the
same interface later; the semantics — put-once, get-by-id, raw kept verbatim — are what matter.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import (
    ActionReceipt,
    ActionRequest,
    EvidenceArtifact,
    Finding,
    SecurityCase,
    SecurityDecision,
    SecurityObservation,
    canonical_json,
    sha256_hex,
)


class IntegrityError(Exception):
    """Raised when an append would overwrite content-addressed evidence with different bytes."""


@dataclass
class CaseStore:
    raw: dict = field(default_factory=dict)          # content_ref -> raw string (verbatim)
    evidence: dict = field(default_factory=dict)     # artifact_id -> EvidenceArtifact
    observations: dict = field(default_factory=dict) # obs id -> SecurityObservation
    findings: dict = field(default_factory=dict)     # finding_id -> Finding
    cases: dict = field(default_factory=dict)        # case_id -> SecurityCase
    actions: dict = field(default_factory=dict)      # request_id -> ActionRequest
    decisions: dict = field(default_factory=dict)    # decision_id -> SecurityDecision
    receipts: dict = field(default_factory=dict)     # receipt_id -> ActionReceipt

    # ── evidence (append-once, content-addressed) ──
    def put_evidence(self, ev: EvidenceArtifact, raw) -> EvidenceArtifact:
        raw_s = raw if isinstance(raw, str) else canonical_json(raw)
        if sha256_hex(raw_s) != ev.sha256:
            raise IntegrityError(f"{ev.artifact_id}: raw content does not match declared sha256")
        existing = self.raw.get(ev.content_ref)
        if existing is not None and existing != raw_s:
            raise IntegrityError(f"{ev.content_ref}: append would overwrite different evidence bytes")
        self.raw[ev.content_ref] = raw_s
        self.evidence[ev.artifact_id] = ev
        return ev

    def get_raw(self, ev: EvidenceArtifact):
        return self.raw.get(ev.content_ref)

    # ── the rest are put-by-id (immutable objects; same id ⇒ same content) ──
    def put_observation(self, o: SecurityObservation) -> SecurityObservation:
        self.observations[o.id] = o; return o

    def put_finding(self, f: Finding) -> Finding:
        self.findings[f.finding_id] = f; return f

    def put_action(self, a: ActionRequest) -> ActionRequest:
        self.actions[a.request_id] = a; return a

    def put_decision(self, d: SecurityDecision) -> SecurityDecision:
        self.decisions[d.decision_id] = d; return d

    def put_receipt(self, r: ActionReceipt) -> ActionReceipt:
        self.receipts[r.receipt_id] = r; return r

    def put_case(self, c: SecurityCase) -> SecurityCase:
        self.cases[c.case_id] = c; return c

    def case_bundle(self, case_id: str) -> dict:
        """Everything a case references, resolved — the payload a case view / replay reads. Receipts are
        resolved by the case's action request ids (a receipt is proof for one ActionRequest)."""
        c = self.cases[case_id]
        case_actions = set(c.action_refs)
        return {
            "case": c.to_dict(),
            "evidence": [self.evidence[e].to_dict() for e in c.evidence_refs if e in self.evidence],
            "observations": [self.observations[o].to_dict() for o in c.observation_refs if o in self.observations],
            "findings": [self.findings[f].to_dict() for f in c.finding_refs if f in self.findings],
            "actions": [self.actions[a].to_dict() for a in c.action_refs if a in self.actions],
            "decisions": [self.decisions[d].to_dict() for d in c.decision_refs if d in self.decisions],
            "receipts": [r.to_dict() for r in self.receipts.values() if r.request_id in case_actions],
        }
