"""Saga runner: explicit-only compensation with its own request/authorization/receipt/verification events.

``run_saga(rt, mission, plan)`` is what ``MissionRuntime._saga`` delegates to. It never assumes a step is
reversible, never reports a mission as rolled back because a compensating action was *attempted*, and leaves
irreversible steps alone (listed as ``not_reversible``). Enforcement of "the forward step really executed",
"the undo is the registered one" and "the compensation has its own approval" stays in
``Executor.compensate`` (docs/APPROVAL_TOKENS.md, docs/COMPENSATION.md).

Per attempt ``n`` (counted from the log): idempotency key ``{key}:undo:{n}``, read-back key
``{key}:undo:{n}:verify``. Attempts are capped (``AGENTIC_OS_COMPENSATION_MAX_ATTEMPTS``, default 3); after the
cap only a human ``acknowledge`` resolves the node. A per-(mission,node) lock serialises concurrent callers.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import compensation as C
from .approval import inputs_hash
from .control_auth import approver_tokens
from .compensation_policy import FailurePolicy, effective_policy, is_forked
from .types import NodeState
from .verify import is_staged, result_failed

OUTCOME_KEYS = ("compensation_incomplete", "compensation_remaining", "compensated", "compensated_unverified",
                "compensation_acknowledged", "compensation_skipped", "not_reversible", "not_compensated",
                "rolled_back", "failure_policy", "partially_completed", "retained_effects", "incomplete_steps")
APPROVAL_TTL_S = 3600

_LOCKS: dict[tuple, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(mission_id: str, node_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((mission_id, node_id), threading.Lock())


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


def _verify(rt, m, node, spec, forward_result, undo_result, attempt) -> "tuple[bool | None, str, str]":
    """-> (verified, method, detail). Only a declared read-back that returns an explicit ``verified: true``
    PLUS the observed state (``observed``/``digest``) verifies. Anything else is unverified/failed. A bare
    operator ack of the undo is NOT verification. Verify capabilities are trusted code owned by the same
    operator as the forward capability; a handler that lies cannot be detected generically.

    TRUST LIMIT (docs/COMPENSATION.md): `observed`/`digest` are checked for PRESENCE only. They are not
    compared with the logged forward-result digest or a declared expected state, because no capability
    declares one yet; a verify capability that returns `verified: true` with any non-empty observation is
    believed. Treat `rolled_back` as "the operator's own read-back said so", not an independent audit."""
    cs = C.coerce(getattr(spec, "compensation", None)) if spec is not None else None
    if cs is None or not cs.verify:
        return None, "", "compensation declares no `verify` read-back"
    vcap = rt.registry.get(cs.verify)
    if vcap is None or getattr(vcap, "side_effecting", False):
        return False, "readback", f"verify '{cs.verify}' is not a registered read-only capability"
    try:
        out = rt._executor_for(m).client.invoke(
            vcap.operator or node.operator, cs.verify,
            {"verify_undo_of": node.capability, "result": forward_result, "undo_result": undo_result,
             "_node": f"{node.id}:undo:verify", "_mission": m.id},
            f"{node.idempotency_key}:undo:{attempt}:verify")
    except Exception as e:  # noqa: BLE001
        return False, "readback", f"read-back raised: {e}"
    bad = _fail_reason(out)
    if bad:
        return False, "readback", bad
    if not isinstance(out, dict) or out.get("verified") is not True:
        return False, "readback", "read-back did not return an explicit `verified: true`"
    if not (out.get("observed") not in (None, "", [], {}) or out.get("digest")):
        return False, "readback", "read-back did not echo the observed state (`observed` or `digest`)"
    return True, "readback", ""


def _check_preconditions(rt, m, node, spec, forward_result, attempt, phase) -> str:
    """'' when the declared ``compensation.preconditions`` read-back says reversing is safe NOW, else why not.
    Fail closed: an unregistered / side-effecting / raising / unstaged / non-explicit check is "not safe". A
    capability that declares no precondition is not evaluated here (``full_declaration_problems`` demands one
    of every compensatable action an owner wants guarded)."""
    cs = C.coerce(getattr(spec, "compensation", None)) if spec is not None else None
    if cs is None or not cs.preconditions:
        return ""
    pcap = rt.registry.get(cs.preconditions)
    if pcap is None or getattr(pcap, "side_effecting", False):
        return f"precondition_unevaluable: '{cs.preconditions}' is not a registered read-only capability"
    try:
        out = rt._executor_for(m).client.invoke(
            pcap.operator or node.operator, cs.preconditions,
            {"precondition_for_undo_of": node.capability, "undo": node.undo, "result": forward_result,
             "_node": f"{node.id}:undo:precondition", "_mission": m.id},
            f"{node.idempotency_key}:undo:{attempt}:precondition:{phase}")
    except Exception as e:  # noqa: BLE001
        return f"precondition_unevaluable: check raised: {e}"
    bad = _fail_reason(out)
    if bad:
        return f"precondition_unevaluable: {bad}"
    if not isinstance(out, dict) or out.get("holds") is not True:
        why = out.get("reason") if isinstance(out, dict) else ""
        return f"precondition_not_met: {why or 'the check did not return an explicit `holds: true`'}"
    return ""


def run_saga(rt, m, plan) -> None:
    store = rt.store
    status = rt.repo.node_status(m.id)
    policy = effective_policy(m, forked=is_forked(plan))
    forward_caps = {n.capability for n in plan.graph.nodes}
    if policy is FailurePolicy.COMPENSATE_DECLARED:
        for node in reversed(plan.graph.nodes):
            if status.get(node.id) != NodeState.DONE or not node.side_effecting or not node.undo:
                continue
            with _lock(m.id, node.id):
                _compensate_node(rt, m, node, policy, forward_caps)
    settle(rt, m, plan)


def _compensate_node(rt, m, node, policy, forward_caps) -> None:
    store = rt.store
    st = C.fold_compensation(store.for_mission(m.id)).get(node.id)
    if st and st["status"] in ("compensated", "executed_unverified", "acknowledged"):
        return                                               # compensate once: never a second time
    spec = rt.registry.get(node.capability)
    if C.is_preserved(spec):
        return                                               # PER-ACTION: this effect survives a failed workflow
    pending_approval = bool(st and st["status"] == "approval_required")
    if st and st["status"] in ("failed", "skipped") and st["attempts"] >= C.max_attempts():
        return                                               # exhausted: only a human acknowledge clears it
    if pending_approval and not st.get("approval"):
        return                                               # parked, still waiting for the human
    attempt = st["attempt"] if pending_approval else (st["attempts"] + 1 if st else 1)
    key = f"{node.idempotency_key}:undo:{attempt}"
    cs = C.coerce(getattr(spec, "compensation", None)) if spec is not None else None
    base = {"node_id": node.id, "capability": node.capability, "undo": node.undo, "idempotency_key": key,
            "attempt": attempt}
    ev: dict = {}
    try:
        ev = rt._compensation_evidence(m.id, node.id) or {}
    except Exception:  # noqa: BLE001
        pass
    requires = bool(cs.requires_approval) if cs is not None else False
    if not pending_approval:
        store.append(C.EV_REQUESTED, m.id, {**base, "policy": policy.value,
                                            "reversibility": C.Reversibility.REVERSIBLE_EXPLICIT.value})
        if rt.registry.get(node.undo) is None:
            store.append(C.EV_UNDECLARED_TARGET, m.id, {
                **base, "warning": f"declared undo '{node.undo}' is not a registered capability; it will not "
                                   "receive the compensation marker (sunset: see docs/COMPENSATION.md)"})
        if ev.get("executed") and node.undo != node.capability and node.undo not in forward_caps:
            # evaluate the declared compensation against CURRENT conditions BEFORE asking anyone to approve it
            why = _check_preconditions(rt, m, node, spec, ev.get("result"), attempt, "plan")
            if why:
                store.append(C.EV_SKIPPED, m.id, {**base, "reason": why})
                return                                       # NOT executed: the effect is retained and reported
        if requires and ev.get("executed") and node.undo != node.capability and node.undo not in forward_caps:
            inputs = C.undo_inputs(node, m.id, ev.get("result"))
            store.append(C.EV_APPROVAL_REQUIRED, m.id, {
                **base, "inputs_sha256": inputs_hash(inputs), "undo_inputs": inputs["result"],
                "message": f"approve compensation of {node.id}: run '{node.undo}' with exactly these inputs"})
            return                                           # PARK: needs its own human approval
    approval = st.get("approval") if pending_approval else None
    if pending_approval and ev.get("executed") and node.undo not in forward_caps:
        # the world may have changed while the approval was pending: re-check right before executing
        why = _check_preconditions(rt, m, node, spec, ev.get("result"), attempt, "exec")
        if why:
            store.append(C.EV_SKIPPED, m.id, {**base, "reason": why})
            return
    registered = rt._registered_undo(node.capability)
    if node.undo in forward_caps:
        out, reason = {"compensation_skipped": True, "undo_error": "compensation_target_is_forward_action"}, \
            f"compensation_target_is_forward_action: '{node.undo}' is also a forward action of this plan"
    else:
        if ev.get("executed") and registered == node.undo and (not requires or approval):
            store.append(C.EV_AUTHORIZED, m.id, {
                **base, "basis": "own_human_approval" if requires else "evidence_bound_marker",
                "forward_executed": True, "forward_approved": bool(ev.get("approved")),
                "requires_approval": requires, "registered_undo": registered,
                "approved_by": (approval or {}).get("approver")})
        reason, out = "", None
        try:
            out = rt._executor_for(m).compensate(node, mission_id=m.id, attempt=attempt, approval=approval)
        except Exception as e:  # noqa: BLE001 — best effort; never raise past the caller
            out, reason = {"undo_error": str(e)}, f"undo raised: {e}"
        reason = reason or _fail_reason(out)
    if not reason:
        store.append(C.EV_EXECUTED, m.id, {**base, "receipt": C.receipt_for(node.id, node.undo, key, out),
                                           "result": out})
        verified, method, detail = _verify(rt, m, node, spec, ev.get("result"), out, attempt)
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


# ── outcome: ONE function used live (settle) and on rehydrate, so replay equals the live outcome ─────────────
def _note(spec) -> str:
    return C.irreversible_note(spec) if spec is not None else ""


def compute_outcome(rt, m, plan) -> dict:
    status = rt.repo.node_status(m.id)
    events = rt.store.for_mission(m.id)
    folded = C.fold_compensation(events)
    s = C.summarize(folded)
    policy = effective_policy(m, forked=is_forked(plan))
    not_reversible: list[dict] = []
    not_compensated: list[str] = []
    retained: list[dict] = []       # EVERY consequential effect that is still in place, with why
    for node in reversed(plan.graph.nodes):
        if status.get(node.id) != NodeState.DONE or not node.side_effecting:
            continue
        spec = rt.registry.get(node.capability)
        preserved = C.is_preserved(spec)
        st = folded.get(node.id, {})
        if not node.undo:
            cls = C.reversibility_of(spec) if spec is not None else C.Reversibility.UNKNOWN
            if cls is C.Reversibility.NOT_APPLICABLE:
                cls = C.Reversibility.UNKNOWN
            if getattr(node, "irreversible", False):
                cls = C.Reversibility.IRREVERSIBLE
            entry = {"node_id": node.id, "capability": node.capability, "reversibility": cls.value}
            if preserved:
                entry["disposition"] = C.PRESERVE
            not_reversible.append(entry)
            retained.append({"node_id": node.id, "capability": node.capability,
                             "reason": "preserved_by_declaration" if preserved else
                             ("irreversible" if cls is C.Reversibility.IRREVERSIBLE else "undeclared_reversibility"),
                             "detail": _note(spec)})
            continue
        if preserved or policy is not FailurePolicy.COMPENSATE_DECLARED:
            not_compensated.append(node.id)
            retained.append({"node_id": node.id, "capability": node.capability,
                             "reason": "preserved_by_declaration" if preserved else "left_by_policy",
                             "detail": _note(spec) or policy.value})
            continue
        reason = {"skipped": "precondition_not_met", "failed": "compensation_failed",
                  "approval_required": "awaiting_compensation_approval",
                  "executed_unverified": "compensation_unverified", "acknowledged": "acknowledged"}.get(
                      st.get("status", ""))
        if reason:
            retained.append({"node_id": node.id, "capability": node.capability, "reason": reason,
                             "detail": st.get("reason") or st.get("ack_reason") or ""})
    remaining = list(s["compensation_failed"])
    # an exhausted (max attempts) failure is still "remaining": only an acknowledge clears it
    out: dict = {"failure_policy": policy.value, "compensated": s["compensated"],
                 "compensated_unverified": s["compensated_unverified"],
                 "compensation_acknowledged": s["compensation_acknowledged"],
                 "not_reversible": not_reversible, "not_compensated": not_compensated}
    if s["compensation_skipped"]:
        out["compensation_skipped"] = dict(s["compensation_skipped"])
    if remaining:
        out.update(compensation_incomplete=True, compensation_remaining=remaining)
    # the workflow did not complete: which steps never finished (retry / escalate these), and what remains
    incomplete = []
    for node in plan.graph.nodes:
        stt = status.get(node.id)
        if stt in (NodeState.DONE, NodeState.COMPENSATED):
            continue
        err = next((e.payload.get("error") or e.payload.get("reason") for e in reversed(events)
                    if e.type == "NodeFailed" and e.payload.get("node_id") == node.id), None)
        incomplete.append({"node_id": node.id, "capability": node.capability,
                           "status": getattr(stt, "value", None) or "not_run", **({"error": str(err)} if err else {})})
    if retained:
        out["retained_effects"] = retained
        # PARTIALLY_COMPLETED: some effects remain after a workflow that did not finish. Never "rolled back".
        out["partially_completed"] = bool(incomplete)
        if incomplete:
            out["incomplete_steps"] = incomplete
    # `rolled_back` only when every executed consequential effect was VERIFIED undone by a real read-back and
    # nothing is left over (unverified, failed, acknowledged-as-remaining, irreversible, preserved or left).
    out["rolled_back"] = bool(s["compensated"] and s["readback_only"]
                              and not (remaining or s["compensated_unverified"] or s["compensation_acknowledged"]
                                       or not_reversible or not_compensated or retained))
    return out


def _record_retained(rt, m, outcome: dict) -> None:
    """Append the retained-effects record to the durable log (execution history) when it CHANGED, so the
    history names every effect a failed workflow left behind and why, once per distinct state."""
    retained = outcome.get("retained_effects") or []
    if not retained:
        return
    last = next((e.payload for e in reversed(rt.store.for_mission(m.id)) if e.type == C.EV_RETAINED), None)
    if last is not None and last.get("retained_effects") == retained and \
            last.get("incomplete_steps") == outcome.get("incomplete_steps"):
        return
    rt.store.append(C.EV_RETAINED, m.id, {
        "partially_completed": bool(outcome.get("partially_completed")), "retained_effects": retained,
        "incomplete_steps": outcome.get("incomplete_steps") or [], "failure_policy": outcome.get("failure_policy")})


def settle(rt, m, plan) -> None:
    """Recompute the compensation part of ``m.outcome`` from the log."""
    out = {k: v for k, v in (m.outcome or {}).items() if k not in OUTCOME_KEYS}
    out.update(compute_outcome(rt, m, plan))
    m.outcome = out
    _record_retained(rt, m, out)


def outcome_from_log(rt, mission_id: str) -> dict:
    """Compensation outcome fields rebuilt from the durable log (rehydrate / explain)."""
    m = rt._missions.get(mission_id)
    plan = rt._plans.get(mission_id)
    if m is None or plan is None:
        return {}
    return compute_outcome(rt, m, plan)


# ── operator interventions (reached only through the authenticated routes / guarded runtime method) ──────────
class InterventionError(ValueError):
    """Wrong state / unknown node / nothing to do. Routes map this to 409/404."""


def intervene(rt, m, action: str, *, actor: str, reason: str = "", node_id: str | None = None) -> None:
    folded = C.fold_compensation(rt.store.for_mission(m.id))
    plan = rt._plans[m.id]
    now = time.time()
    if action == "retry":
        retryable = [n for n, v in folded.items() if v["status"] in ("failed", "skipped")
                     and v["attempts"] < C.max_attempts() and (node_id is None or n == node_id)]
        if not retryable:
            raise InterventionError("no failed compensation is retryable (none failed, or max attempts reached: "
                                    "acknowledge instead)")
        rt.store.append("CompensationRetryRequested", m.id, {"actor": actor, "reason": reason, "node_ids": retryable,
                                                             "at": now})
        run_saga(rt, m, plan)
    elif action == "approve":
        v = folded.get(node_id or "")
        if not v or v["status"] != "approval_required":
            raise InterventionError("that node has no compensation awaiting approval")
        # separation of duty: with MORE THAN ONE approver credential configured, the identity that approved
        # the forward step may not also approve its compensation. Identity granularity is role:tenant (or
        # "admin"), so this bites only across distinct approver identities; admin is exempt (single operator).
        if actor != "admin" and len(approver_tokens()) > 1:
            fwd = next((e.payload.get("approver") for e in rt.store.for_mission(m.id)
                        if e.type == "ApprovalGranted" and e.payload.get("node_id") == node_id), None)
            if fwd and fwd == actor:
                raise InterventionError("the approver of the forward step may not approve its compensation "
                                        "(separation of duty); another approver identity must")
        node = plan.graph.by_id(node_id)
        ev = rt._compensation_evidence(m.id, node_id) or {}
        inputs = C.undo_inputs(node, m.id, ev.get("result"))
        digest = inputs_hash(inputs)
        if digest != v.get("inputs_sha256"):
            raise InterventionError("the compensation inputs changed since approval was requested")
        rt.store.append(C.EV_APPROVED, m.id, {
            "node_id": node_id, "mission_id": m.id, "undo": node.undo, "attempt": v["attempt"],
            "inputs_sha256": digest, "approver": actor, "reason": reason, "at": now, "exp": now + APPROVAL_TTL_S})
        run_saga(rt, m, plan)
    elif action in ("acknowledge", "verify_manually"):
        want = ("failed", "approval_required", "executed_unverified", "skipped") if action == "acknowledge" \
            else ("executed_unverified",)
        targets = [n for n, v in folded.items() if v["status"] in want and (node_id is None or n == node_id)]
        if not targets:
            raise InterventionError(f"nothing to {action.replace('_', ' ')}")
        for nid in targets:
            if action == "acknowledge":
                rt.store.append(C.EV_ACKNOWLEDGED, m.id, {"node_id": nid, "actor": actor, "reason": reason,
                                                          "undo": folded[nid].get("undo"), "at": now})
            else:
                rt.store.append(C.EV_VERIFIED, m.id, {"node_id": nid, "undo": folded[nid].get("undo"),
                                                      "method": "manual", "actor": actor, "reason": reason,
                                                      "at": now})
        settle(rt, m, plan)
    else:
        raise InterventionError(f"unknown compensation action {action!r}")


def attention_items(rt, mission_id: str) -> list[dict]:
    """needs-you items for compensation that needs a human."""
    folded = C.fold_compensation(rt.store.for_mission(mission_id))
    goal = rt.repo.goal(mission_id)
    items: list[dict] = []
    for nid, v in sorted(folded.items()):
        base = {"mission_id": mission_id, "node_id": nid, "goal": goal}
        if v["status"] == "approval_required":
            req = next((e for e in reversed(rt.store.for_mission(mission_id))
                        if e.type == C.EV_APPROVAL_REQUIRED and e.payload.get("node_id") == nid), None)
            p = req.payload if req else {}
            items.append({**base, "kind": "compensation_approval", "title": f"Approve compensation of {nid}",
                          "why": f"run '{v.get('undo')}' (attempt {v['attempt']}) with exactly these inputs",
                          "undo": v.get("undo"), "inputs": p.get("undo_inputs"),
                          "inputs_sha256": p.get("inputs_sha256"), "decisions": ["approve_compensation", "acknowledge"]})
        elif v["status"] == "failed":
            exhausted = v["attempts"] >= C.max_attempts()
            items.append({**base, "kind": "compensation_failed", "title": f"Compensation failed: {v.get('undo')}",
                          "why": v.get("reason", ""), "attempts": v["attempts"],
                          "decisions": ["acknowledge"] if exhausted else ["retry_compensation", "acknowledge"]})
        elif v["status"] == "skipped":
            items.append({**base, "kind": "compensation_skipped", "title": f"Compensation not safe now: {v.get('undo')}",
                          "why": v.get("reason", ""), "decisions": ["retry_compensation", "acknowledge"]})
        elif v["status"] == "executed_unverified":
            items.append({**base, "kind": "compensation_unverified", "title": f"Verify compensation: {v.get('undo')}",
                          "why": "the compensating action ran but nothing proved the effect is gone",
                          "decisions": ["verify_manually", "acknowledge"]})
    # A FAILED mission that kept effects in place (policy left them, a declaration PRESERVEs them, a precondition
    # held them back): surface them with the steps that never finished, so a human retries or escalates.
    m = rt._missions.get(mission_id)
    out = (m.outcome or {}) if m is not None else {}
    left = list(out.get("not_compensated") or [])
    actionable = [r for r in (out.get("retained_effects") or [])
                  if r.get("reason") in ("preserved_by_declaration", "precondition_not_met", "left_by_policy")]
    if m is not None and m.state.value == "failed" and (left or actionable) and not out.get("compensation_acknowledged"):
        items.append({"mission_id": mission_id, "goal": goal, "kind": "effects_left_in_place",
                      "title": f"Mission partially completed; {len(left) or len(actionable)} effect(s) were left in place",
                      "why": f"failure policy {out.get('failure_policy')}: succeeded effects are kept, not reversed",
                      "not_compensated": left, "not_reversible": list(out.get("not_reversible") or []),
                      "retained_effects": list(out.get("retained_effects") or []),
                      "incomplete_steps": list(out.get("incomplete_steps") or []),
                      "decisions": ["manual_remediation", "escalate"]})
    return items
