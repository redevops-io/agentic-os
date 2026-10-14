"""Saga runner: explicit-only compensation with its own request/authorization/receipt/verification events.

``run_saga(rt, mission, plan)`` is what ``MissionRuntime._saga`` delegates to. It never assumes a step is
reversible, never reports a mission as rolled back because a compensating action was *attempted*, and leaves
irreversible steps alone (listed as ``not_reversible``). Enforcement of "the forward step really executed"
and "the undo is the registered one" stays in ``Executor.compensate`` (docs/APPROVAL_TOKENS.md).
"""
from __future__ import annotations

from typing import Any

from . import compensation as C
from .compensation_policy import FailurePolicy, effective_policy
from .types import MissionState, NodeState
from .verify import is_staged, result_failed

OUTCOME_KEYS = ("compensation_incomplete", "compensation_remaining", "compensated", "compensated_unverified",
                "not_reversible", "not_compensated", "rolled_back", "failure_policy")


def _fail_reason(out: Any) -> str:
    """'' when the undo result looks like a real success, else why not."""
    if out is None:
        return "undo returned nothing"
    if isinstance(out, dict) and out.get("undo_error"):
        return f"undo raised: {out['undo_error']}"
    if isinstance(out, dict) and is_staged(out):
        return result_failed(out)[1] or "undo staged (pending approval), not executed"
    failed, why = result_failed(out)
    return why if failed else ""


def _verify(rt, m, node, spec, forward_result, undo_result) -> "tuple[bool | None, str, str]":
    """-> (verified, method, detail). True/False when a check ran; None = nothing declared to check with."""
    cs = C.coerce(getattr(spec, "compensation", None)) if spec is not None else None
    if cs is not None and cs.verify:
        vcap = rt.registry.get(cs.verify)
        operator = (vcap.operator if vcap is not None else None) or node.operator
        try:
            out = rt._executor_for(m).client.invoke(
                operator, cs.verify,
                {"verify_undo_of": node.capability, "result": forward_result, "undo_result": undo_result,
                 "_node": node.id, "_mission": m.id},
                f"{node.idempotency_key}:undo:verify")
        except Exception as e:  # noqa: BLE001
            return False, "readback", f"read-back raised: {e}"
        bad = _fail_reason(out) or (isinstance(out, dict) and out.get("verified") is False and "read-back reports the effect remains")
        return (False, "readback", str(bad)) if bad else (True, "readback", "")
    if cs is None:
        # LEGACY undo-only capability (no `compensation` declaration): the operator's own successful result is
        # the only signal. Recorded as the weaker `operator_ack`; annotate the capability to get a real check.
        return True, "operator_ack", ""
    return None, "", "compensation declares no `verify` read-back"


def run_saga(rt, m, plan) -> None:
    store = rt.store
    status = rt.repo.node_status(m.id)
    folded = C.fold_compensation(store.for_mission(m.id))
    policy = effective_policy(m, forked=bool(getattr(plan.graph, "forks", None)))
    not_reversible: list[dict] = []
    not_compensated: list[str] = []
    for node in reversed(plan.graph.nodes):
        if status.get(node.id) != NodeState.DONE or not node.side_effecting:
            continue
        spec = rt.registry.get(node.capability)
        if not node.undo:
            cls = C.reversibility_of(spec) if spec is not None else C.Reversibility.UNKNOWN
            if cls is C.Reversibility.NOT_APPLICABLE:        # registry says inert though the node claims effects
                cls = C.Reversibility.UNKNOWN
            if getattr(node, "irreversible", False):
                cls = C.Reversibility.IRREVERSIBLE
            not_reversible.append({"node_id": node.id, "capability": node.capability, "reversibility": cls.value})
            continue
        if policy is not FailurePolicy.COMPENSATE_DECLARED:
            not_compensated.append(node.id)                  # reversible, but the policy says report only
            continue
        if folded.get(node.id, {}).get("status") in ("compensated", "executed_unverified"):
            continue                                         # compensate once: never a second time
        _compensate_node(rt, m, node, spec, policy)
    _finalize(rt, m, policy, not_reversible, not_compensated)


def _compensate_node(rt, m, node, spec, policy) -> None:
    store = rt.store
    key = f"{node.idempotency_key}:undo"
    cs = C.coerce(getattr(spec, "compensation", None)) if spec is not None else None
    base = {"node_id": node.id, "capability": node.capability, "undo": node.undo, "idempotency_key": key}
    store.append(C.EV_REQUESTED, m.id, {**base, "policy": policy.value,
                                        "reversibility": C.Reversibility.REVERSIBLE_EXPLICIT.value})
    ev: dict = {}
    try:
        ev = rt._compensation_evidence(m.id, node.id) or {}
    except Exception:  # noqa: BLE001
        pass
    requires = bool(cs.requires_approval) if cs is not None else False
    registered = rt._registered_undo(node.capability)
    if ev.get("executed") and registered == node.undo and (not requires or ev.get("approved")):
        store.append(C.EV_AUTHORIZED, m.id, {
            **base, "basis": "evidence_bound_marker", "forward_executed": True,
            "forward_approved": bool(ev.get("approved")), "requires_approval": requires,
            "registered_undo": registered})
    reason, out = "", None
    try:
        out = rt._executor_for(m).compensate(node, mission_id=m.id)
    except Exception as e:  # noqa: BLE001 — best effort; never raise past the caller
        out, reason = {"undo_error": str(e)}, f"undo raised: {e}"
    reason = reason or _fail_reason(out)
    if not reason:
        store.append(C.EV_EXECUTED, m.id, {**base, "receipt": C.receipt_for(node.id, node.undo, key, out),
                                           "result": out})
        verified, method, detail = _verify(rt, m, node, spec, ev.get("result"), out)
        if verified is True:
            store.append(C.EV_VERIFIED, m.id, {**base, "method": method})
            store.append("NodeCompensated", m.id, {"node_id": node.id, "undo": node.undo, "result": out,
                                                   "verification": method})
            return
        if verified is None:
            return                                           # executed, UNVERIFIED: not reported as compensated
        reason = f"verification_failed: {detail}"
    store.append(C.EV_FAILED, m.id, {**base, "reason": reason, "result": out})
    # legacy event kept: explain()/needs-you/old consumers read it
    store.append("NodeCompensationFailed", m.id, {"node_id": node.id, "undo": node.undo, "reason": reason,
                                                  "result": out})


def _finalize(rt, m, policy, not_reversible, not_compensated) -> None:
    folded = C.fold_compensation(rt.store.for_mission(m.id))
    s = C.summarize(folded)
    remaining = list(s["compensation_failed"])
    out = dict(m.outcome or {})
    out.update({"failure_policy": policy.value, "compensated": s["compensated"],
                "compensated_unverified": s["compensated_unverified"],
                "not_reversible": not_reversible, "not_compensated": not_compensated})
    if remaining:
        out.update(compensation_incomplete=True, compensation_remaining=remaining)
    else:
        out.pop("compensation_incomplete", None)
        out.pop("compensation_remaining", None)
    # `rolled_back` only when every executed consequential effect was VERIFIED undone and nothing is left over.
    out["rolled_back"] = bool(s["compensated"]) and not (remaining or s["compensated_unverified"]
                                                          or not_reversible or not_compensated)
    m.outcome = out


def outcome_from_log(rt, mission_id: str) -> dict:
    """Compensation outcome fields rebuilt from the durable log (rehydrate / explain)."""
    folded = C.fold_compensation(rt.store.for_mission(mission_id))
    if not folded:
        return {}
    s = C.summarize(folded)
    out = {"compensated": s["compensated"], "compensated_unverified": s["compensated_unverified"]}
    if s["compensation_failed"]:
        out.update(compensation_incomplete=True, compensation_remaining=list(s["compensation_failed"]))
    return out


def attention_items(rt, mission_id: str) -> list[dict]:
    """needs-you items for compensation that needs a human: failed compensations and unverified ones."""
    folded = C.fold_compensation(rt.store.for_mission(mission_id))
    items: list[dict] = []
    for nid, v in sorted(folded.items()):
        if v["status"] == "failed":
            items.append({"mission_id": mission_id, "node_id": nid, "kind": "compensation_failed",
                          "title": f"Compensation failed: {v.get('undo')}", "why": v.get("reason", ""),
                          "goal": rt.repo.goal(mission_id), "decisions": ["retry_compensation", "acknowledge"]})
        elif v["status"] == "executed_unverified":
            items.append({"mission_id": mission_id, "node_id": nid, "kind": "compensation_unverified",
                          "title": f"Verify compensation: {v.get('undo')}",
                          "why": "the compensating action ran but nothing proved the effect is gone",
                          "goal": rt.repo.goal(mission_id), "decisions": ["verify_manually", "acknowledge"]})
    return items
