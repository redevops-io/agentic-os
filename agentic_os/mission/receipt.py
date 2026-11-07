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
    stale: List[Dict[str, Any]] = []
    for e in rt.repo.timeline(mission_id):
        t, p = e.get("type"), e.get("payload") or {}
        if t == "StaleResultRecorded":          # a superseded executor's late answer: a possible duplicate effect, never an outcome
            stale.append({"seq": e.get("seq"), "node_id": p.get("node_id"), "capability": p.get("capability"), "kind": p.get("kind"),
                          "terminal": bool(p.get("terminal")), "mission_state": p.get("mission_state", "")})
        elif t == "NodeSucceeded":
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
    basis = rt._authority_basis(mission_id) if hasattr(rt, "_authority_basis") else []
    rollback = None
    if folded:                                  # a compensation was attempted, denied or recorded: the receipt says how it classifies
        from .compensation_runner import outcome_from_log  # noqa: PLC0415
        o = outcome_from_log(rt, mission_id)
        rollback = {"rolled_back": o.get("rolled_back", False), "compensation_state": o.get("compensation_state", "none"),
                    "steps": o.get("step_classification", []), "residual_effects": o.get("residual_effects", [])}
    integ = getattr(rt.store, "integrity", None)
    anchor = ({"status": integ.status, "anchored": integ.anchored, "records": integ.count}
              if integ is not None and getattr(integ, "status", "unanchored") != "unanchored" else None)
    return {
        **({"authority": {"basis": basis}} if basis else {}),
        **({"ledger_anchor": anchor} if anchor else {}),     # the tail anchor (tail truncation); absent for a log that has none yet
        **({"stale_results": stale} if stale else {}),      # possible duplicate effects (superseded executors' answers), listed so nobody reads the mission as clean
        **({"rollback": rollback} if rollback else {}),     # what authorised the actions: the owner's envelope and/or a named test policy
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
