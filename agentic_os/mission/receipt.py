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
                              "capability": p.get("capability"), "decision": "approve",
                              "approver": p.get("approver"), "inputs_sha256": p.get("inputs_sha256")})
        elif t == "MissionOutcome":
            outcome = {"kind": p.get("kind"), "success": p.get("success"),
                       "business_value": p.get("business_value")}
    from .compensation import fold_compensation, summarize  # noqa: PLC0415
    folded = fold_compensation(rt.repo.timeline(mission_id))
    state = rt.repo.state(mission_id)
    ledger = rt.evidence.ledger(mission_id) or []
    from .store import verify_chain  # noqa: PLC0415
    chain = verify_chain(list(rt.store.for_mission(mission_id)))
    head = rt.store.head(mission_id) if hasattr(rt.store, "head") else ""
    return {
        "mission_id": mission_id,
        "goal": rt.repo.goal(mission_id),
        "state": (state.value if isinstance(state, MissionState) else state),
        "outcome": outcome,
        "approvals": approvals,
        "steps": steps,
        "compensation": {"nodes": folded, **summarize(folded)},   # own receipts; never inferred as rolled back
        "evidence_refs": [c.get("id") for c in ledger if isinstance(c, dict) and c.get("id")],
        "credential": "broker-mediated — values never logged",
        # tamper evidence: the chain over this mission's events and its head. The head only protects against
        # edits if it is anchored where the log's writer cannot change it (and cannot detect tail truncation alone).
        "ledger_head": head,
        "ledger_chain": {"ok": chain.ok, "checked": chain.checked, "unchained": chain.unchained,
                         **({"first_bad_seq": chain.first_bad_seq, "reason": chain.reason} if not chain.ok else {})},
    }
