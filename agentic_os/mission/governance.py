"""P10 — Governance plane: policy versioning + an auditable console read-model.

The console UI itself is thin — the evidence ledger (P7), approval queue (P8) and recommendations
(P9) already exist. The load-bearing kernel work is **mid-flight policy edits with versioning
semantics**, so deterministic replay and audit stay unambiguous. Every governance mutation is an
event:

    PolicyChanged { previous_version, new_version, actor, reason, effective_at, affected_nodes,
                    ran_under_previous, retroactive, changes, before_grants, after_grants }

Design decisions this encodes:
  * NON-retroactive (default): nodes already terminal ran under the previous version and stay valid
    (append-only — never rewritten). Only not-yet-terminal nodes see the new policy; the verifier and
    policy plane read the mission's live grants, so a revoked grant fails the next check closed.
  * A tightening (revoke) INVALIDATES open approvals on affected nodes — they must be re-approved
    under the new policy.
  * RETROACTIVE: a completed side effect that used a now-revoked grant is a violation → compensate
    (saga) and fail, rather than silently accept state produced without authority.
  * Replay reproduces the ORIGINAL execution because PolicyChanged is in the ordered log with
    effective_at; folding events in order re-applies each version at the point it took effect.

This module is the read side; the runtime owns the mutations (they must touch execution state).
"""
from __future__ import annotations

_GOV_EVENTS = ("PolicyChanged", "MissionSuspended", "MissionResumed",
               "ApprovalInvalidated", "PolicyViolationDetected", "RecommendationPromoted")


class GovernanceLog:
    def __init__(self, store):
        self.store = store

    def policy_version(self, mission_id: str) -> int:
        """Version 1 at creation; +1 per PolicyChanged — folded from the log, so replay-stable."""
        return 1 + sum(1 for e in self.store.for_mission(mission_id) if e.type == "PolicyChanged")

    def policy_history(self, mission_id: str) -> list[dict]:
        return [{"type": e.type, **e.payload} for e in self.store.for_mission(mission_id)
                if e.type == "PolicyChanged"]

    def interventions(self, mission_id: str) -> list[dict]:
        """Every auditable governance action against a mission, in order."""
        return [{"type": e.type, **e.payload} for e in self.store.for_mission(mission_id)
                if e.type in _GOV_EVENTS]
