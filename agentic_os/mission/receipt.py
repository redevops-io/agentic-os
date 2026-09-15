"""Action Receipt — one folded record of what a Mission did (unified-desktop Gap #1).

The mission runtime has no ``ActionReceipt`` type; its durable receipt IS the event log. This helper folds
``NodeSucceeded`` + ``ApprovalGranted`` + ``MissionOutcome`` + the evidence ledger into a single JSON-safe
record for the unified desktop's Action Receipt surface. Because it is built purely from the persisted log,
it is identical before and after a restart/``rehydrate``.

Data-only by construction: it carries authorization, approvals, results and evidence refs — never a secret
value. Credentials are broker-mediated and never enter the mission log, so they cannot appear here.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .types import MissionState


def mission_receipt(rt, mission_id: str) -> Dict[str, Any]:
    """Fold the durable event log for ``mission_id`` into one Action Receipt dict."""
    steps: List[Dict[str, Any]] = []
    approvals: List[Dict[str, Any]] = []
    outcome: Dict[str, Any] | None = None
    for e in rt.repo.timeline(mission_id):
        t, p = e.get("type"), e.get("payload") or {}
        if t == "NodeSucceeded":
            steps.append({"seq": e.get("seq"), "node_id": p.get("node_id"),
                          "capability": p.get("capability"), "result": p.get("result")})
        elif t == "ApprovalGranted":
            approvals.append({"seq": e.get("seq"), "node_id": p.get("node_id"),
                              "capability": p.get("capability"), "decision": "approve"})
        elif t == "MissionOutcome":
            outcome = {"kind": p.get("kind"), "success": p.get("success"),
                       "business_value": p.get("business_value")}
    state = rt.repo.state(mission_id)
    ledger = rt.evidence.ledger(mission_id) or []
    return {
        "mission_id": mission_id,
        "goal": rt.repo.goal(mission_id),
        "state": (state.value if isinstance(state, MissionState) else state),
        "outcome": outcome,
        "approvals": approvals,
        "steps": steps,
        "evidence_refs": [c.get("id") for c in ledger if isinstance(c, dict) and c.get("id")],
        "credential": "broker-mediated — values never logged",
    }
