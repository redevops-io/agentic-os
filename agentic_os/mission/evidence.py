"""P7A — the Evidence Plane: an append-only kernel primitive over the mission event store.

Evidence, claims and verification results are first-class because verification, explanation,
approvals, replay and learning all read from here — not because a UI needs them. Every record is
an event, so the ledger is durable, replayable and audit-grade for free.
"""
from __future__ import annotations

from .types import Claim, EvidenceRecord, VerificationResult, to_jsonable


class EvidenceLog:
    def __init__(self, store):
        self.store = store

    def raise_claim(self, mission_id: str, claim: Claim) -> Claim:
        self.store.append("ClaimRaised", mission_id, to_jsonable(claim))
        return claim

    def record_evidence(self, mission_id: str, ev: EvidenceRecord) -> EvidenceRecord:
        self.store.append("EvidenceRecorded", mission_id, to_jsonable(ev))
        return ev

    def record_verification(self, mission_id: str, node_id: str, vr: VerificationResult) -> None:
        self.store.append("VerificationRecorded", mission_id,
                          {"node_id": node_id, **to_jsonable(vr)})

    def ledger(self, mission_id: str) -> list[dict]:
        """The full evidence ledger for a mission (claims · evidence · verifications), in order."""
        kinds = {"ClaimRaised", "EvidenceRecorded", "VerificationRecorded"}
        return [{"type": e.type, **e.payload} for e in self.store.for_mission(mission_id)
                if e.type in kinds]
