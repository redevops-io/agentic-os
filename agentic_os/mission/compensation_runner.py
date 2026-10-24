"""Saga runner: explicit-only compensation with its own request/authorization/receipt/verification events.

``run_saga(rt, mission, plan)`` is what ``MissionRuntime._saga`` delegates to. It never assumes a step is
reversible, never reports a mission as rolled back because a compensating action was *attempted*, and leaves
irreversible steps alone (listed as ``not_reversible``). Enforcement of "the forward step really executed",
"the undo is the registered one" and "the compensation has its own approval" stays in
``Executor.compensate`` (docs/SECURITY_MODEL.md, docs/COMPENSATION.md).

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
                "rolled_back", "failure_policy", "partially_completed", "retained_effects", "incomplete_steps",
                "completed_steps", "recovery_actions", "retained_costs", "default_policy_source", "default_policy_warning",
                "legacy_policy_notice")
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
    client = rt._executor_for(m).client
    args = (pcap.operator or node.operator, cs.preconditions,
            {"precondition_for_undo_of": node.capability, "undo": node.undo, "result": forward_result,
             "_node": f"{node.id}:undo:precondition", "_mission": m.id},
            f"{node.idempotency_key}:undo:{attempt}:precondition:{phase}")
    box: dict = {}

    def _call() -> None:
        try:
            box["out"] = client.invoke(*args)
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=_call, daemon=True)
    th.start()
    th.join(C.precondition_timeout_s())
    if th.is_alive():          # a hung read-back must not hang the saga: fail CLOSED (effect retained)
        return f"precondition_unevaluable: check timed out after {C.precondition_timeout_s():g}s"
    if "err" in box:
        return f"precondition_unevaluable: check raised: {box['err']}"
    out = box.get("out")
    bad = _fail_reason(out)
    if bad:
        return f"precondition_unevaluable: {bad}"
    if not isinstance(out, dict) or out.get("holds") is not True:
        why = out.get("reason") if isinstance(out, dict) else ""
        return f"precondition_not_met: {why or 'the check did not return an explicit `holds: true`'}"
    return ""


def _policy_allows(policy, spec) -> bool:
    """May this completed action be considered for compensation under ``policy``? COMPENSATE_DECLARED: any declared
    undo. PRESERVE_ONLY: ONLY a capability that declares ``disposition=CLEANUP`` (a temporary resource; coerce()
    guarantees it also declares preconditions + an authorization policy). Everything else is preserved."""
    if policy is FailurePolicy.COMPENSATE_DECLARED:
        return True
    return policy is FailurePolicy.PRESERVE_ONLY and spec is not None and C.is_cleanup(spec)


LEGACY_NOTICE = ("this mission was created before kernel templates defaulted to PRESERVE_ONLY: its frozen default is "
                 "COMPENSATE_DECLARED, so a failure still auto-reverses declared undos (each still needs its own "
                 "authorization). Set AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1 to re-resolve it to the current default.")


def _legacy_notice(rt, m, policy) -> bool:
    """One-time EVENT (not a behaviour change) for an in-flight mission frozen under the pre-PRESERVE_ONLY default."""
    from .compensation_policy import is_legacy_frozen  # noqa: PLC0415
    if not (is_legacy_frozen(m) and policy is FailurePolicy.COMPENSATE_DECLARED):
        return False
    if not any(e.type == "LegacyFailurePolicyNotice" for e in rt.store.for_mission(m.id)):
        rt.store.append("LegacyFailurePolicyNotice", m.id, {"template": m.template, "frozen_default": "COMPENSATE_DECLARED",
                                                             "notice": LEGACY_NOTICE})
    return True


def maybe_reresolve_legacy(rt, m) -> bool:
    """Operator opt-in (``AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1``) for an in-flight pre-PRESERVE_ONLY mission: re-resolve its
    frozen default to today's. The decision is RECORDED as a ``DefaultPolicyReresolved`` event and re-applied by
    ``rehydrate``, so replay is deterministic and does not depend on the flag being set later. Only a legacy
    COMPENSATE_DECLARED default is touched and only toward the template default (it can never widen)."""
    import os  # noqa: PLC0415
    from .compensation_policy import ENV_RERESOLVE_LEGACY, is_legacy_frozen, resolve_default  # noqa: PLC0415
    if os.environ.get(ENV_RERESOLVE_LEGACY, "").strip().lower() not in ("1", "true", "yes") or not is_legacy_frozen(m):
        return False
    from .templates import TEMPLATE_FAILURE_POLICY  # noqa: PLC0415
    to = TEMPLATE_FAILURE_POLICY.get(m.template or "", "HALT_AND_REPORT")
    rt.store.append("DefaultPolicyReresolved", m.id, {"template": m.template, "from": m.default_failure_policy,
                                                       "to": to, "source": "AGENTIC_OS_RERESOLVE_LEGACY_POLICY"})
    m.default_failure_policy, m.default_policy_source = to, "reresolved"
    return True


def run_saga(rt, m, plan) -> None:
    store = rt.store
    maybe_reresolve_legacy(rt, m)
    status = rt.repo.node_status(m.id)
    policy = effective_policy(m, forked=is_forked(plan))
    forward_caps = {n.capability for n in plan.graph.nodes}
    _legacy_notice(rt, m, policy)
    if policy in (FailurePolicy.COMPENSATE_DECLARED, FailurePolicy.PRESERVE_ONLY):
        for node in reversed(plan.graph.nodes):
            if status.get(node.id) != NodeState.DONE or not node.side_effecting or not node.undo:
                continue
            if not _policy_allows(policy, rt.registry.get(node.capability)):
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
    requires = bool(rt._compensation_requires_approval(node.capability)) if cs is not None else False
    if not pending_approval:
        store.append(C.EV_REQUESTED, m.id, {**base, "policy": policy.value,
                                            "reversibility": C.Reversibility.REVERSIBLE_EXPLICIT.value})
        if rt.registry.get(node.undo) is None:
            store.append(C.EV_UNDECLARED_TARGET, m.id, {
                **base, "warning": f"declared undo '{node.undo}' is not a registered capability; it will not "
                                   "receive the compensation marker (sunset: see docs/COMPENSATION.md)"})
        if node.undo != node.capability and node.undo not in forward_caps:
            # evaluate the declared compensation against CURRENT conditions BEFORE asking anyone to approve it
            why = _check_preconditions(rt, m, node, spec, ev.get("result"), attempt, "plan")
            if why:
                store.append(C.EV_SKIPPED, m.id, {**base, "reason": why})
                return                                       # NOT executed: the effect is retained and reported
        if requires and ev.get("executed") and node.undo != node.capability and node.undo not in forward_caps:
            inputs = C.undo_inputs(node, m.id, ev.get("result"))
            store.append(C.EV_APPROVAL_REQUIRED, m.id, {
                **base, "inputs_sha256": inputs_hash(inputs), "undo_inputs": inputs["result"],
                "cleanup": C.is_cleanup(spec),
                "message": (f"approve CLEANUP of temporary resource {node.id}: run '{node.undo}' with exactly these "
                            "inputs (the rest of the workflow's effects are preserved)") if C.is_cleanup(spec) else
                           f"approve compensation of {node.id}: run '{node.undo}' with exactly these inputs"})
            return                                           # PARK: needs its own human approval
    approval = st.get("approval") if pending_approval else None
    if pending_approval and node.undo not in forward_caps:
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


def _policy_source(m) -> str:
    if getattr(m, "failure_policy", ""):
        return "mission"
    return getattr(m, "default_policy_source", "") or "legacy_frozen"


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
        # REPLAY: what actually happened is in the log. A node with a compensation record is reported from that record
        # whatever the CURRENT policy resolves to (an old log's real compensation must not read as `left_by_policy`).
        if preserved or (not st and not _policy_allows(policy, spec)):
            not_compensated.append(node.id)
            retained.append({"node_id": node.id, "capability": node.capability,
                             "reason": "preserved_by_declaration" if preserved else "left_by_policy",
                             "detail": _note(spec) or policy.value})
            continue
        if not st:
            # eligible (declares an undo, policy compensates) but NO compensation record exists (truncated log,
            # saga not run, cancelled mission): the effect remains and must never read as rolled back
            not_compensated.append(node.id)
            retained.append({"node_id": node.id, "capability": node.capability, "reason": "not_yet_compensated",
                             "detail": "eligible for compensation but no compensation was recorded"})
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
    out: dict = {"failure_policy": policy.value, "default_policy_source": _policy_source(m),
                 "compensated": s["compensated"],
                 "compensated_unverified": s["compensated_unverified"],
                 "compensation_acknowledged": s["compensation_acknowledged"],
                 "not_reversible": not_reversible, "not_compensated": not_compensated}
    if getattr(m, "default_policy_warning", "") and not getattr(m, "failure_policy", ""):
        out["default_policy_warning"] = m.default_policy_warning
    from .compensation_policy import is_legacy_frozen  # noqa: PLC0415
    if is_legacy_frozen(m) and policy is FailurePolicy.COMPENSATE_DECLARED:
        out["legacy_policy_notice"] = LEGACY_NOTICE
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
            out["completed_steps"] = [{"node_id": n.id, "capability": n.capability,
                                       "side_effecting": bool(n.side_effecting)}
                                      for n in plan.graph.nodes if status.get(n.id) == NodeState.DONE]
            out["recovery_actions"] = _recovery_actions(plan, incomplete, retained)
    costs = _retained_costs(rt, m, plan, retained, status)
    if costs:
        out["retained_costs"] = costs
    # `rolled_back` only when every executed consequential effect was VERIFIED undone by a real read-back and
    # nothing is left over (unverified, failed, acknowledged-as-remaining, irreversible, preserved or left).
    out["rolled_back"] = bool(s["compensated"] and s["readback_only"]
                              and not (remaining or s["compensated_unverified"] or s["compensation_acknowledged"]
                                       or not_reversible or not_compensated or retained))
    return out


_ID_KEYS = ("cluster", "service", "target", "resource_id", "resource", "instance_id", "stack", "name", "endpoint")
_HOURLY_KEYS = ("hourly_usd", "estimated_hourly_usd")
_MONTHLY_KEYS = ("est_monthly_usd", "estimated_monthly_usd")


def _num(v) -> "float | None":
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _cost_line(i: dict) -> str:
    who = ", ".join(f"{k}={v}" for k, v in (i.get("resource") or {}).items()) or "identifiers unknown"
    if i["cost_known"]:
        return (f"{i['capability']} [{who}]: still provisioned, ~${i['estimated_hourly_usd']}/h "
                f"(~${i['estimated_monthly_usd']}/month, {i['cost_source']}); stop with {i['action_to_stop_cost'] or 'a manual action'}")
    state = "POSSIBLY provisioned (the step failed)" if i.get("possibly_provisioned") else "still provisioned"
    return (f"{i['capability']} [{who}]: {state}, COST UNKNOWN ({i['cost_source']}); "
            f"check the provider and stop with {i['action_to_stop_cost'] or 'a manual action'}")


def _retained_costs(rt, m, plan, retained: list[dict], status: "dict | None" = None) -> dict:
    """Resources still provisioned (and costing money) after a failed mission: for retained effects, and for a FAILED
    step, whose capability declares ``compensation.incurs_ongoing_cost``. Cost comes ONLY from the forward result
    (``hourly_usd`` / ``est_monthly_usd``, the sky.optimize estimate of the approved placement); with none - or when the
    launch failed over to a different placement than the estimate describes - it is UNKNOWN (``null`` +
    ``cost_known: false``). A number is never invented. A FAILED step may have partly created resources: it is listed as
    ``possibly_provisioned`` with whatever identifiers its inputs carry."""
    status = status if status is not None else rt.repo.node_status(m.id)
    items: list[dict] = []
    cands = [(plan.graph.by_id(r["node_id"]), False) for r in retained]
    cands += [(n, True) for n in plan.graph.nodes if status.get(n.id) in (NodeState.FAILED, NodeState.RUNNING)]
    for node, failed in cands:
        spec = rt.registry.get(node.capability) if node is not None else None
        if spec is None or not C.incurs_ongoing_cost(spec):
            continue
        res: dict = {}
        if not failed:
            try:
                res = (rt._compensation_evidence(m.id, node.id) or {}).get("result")
            except Exception:  # noqa: BLE001
                res = None
        res = res if isinstance(res, dict) else {}
        src = dict(getattr(node, "inputs", None) or {})
        if isinstance(src.get("spec"), dict):
            src = {**src["spec"], **src}
        ids = {k: res[k] for k in _ID_KEYS if res.get(k) not in (None, "", [], {})}
        if failed:
            ids = {k: src[k] for k in _ID_KEYS if isinstance(src.get(k), (str, int)) and src.get(k) != ""}
        hourly = monthly = None
        why = "unknown"
        if failed:
            why = "unknown: the step failed, resources may exist"
        elif res.get("failed_over"):
            why = "unknown: launch failed over, the approved estimate describes another placement"
        else:
            hourly = next((v for v in (_num(res.get(k)) for k in _HOURLY_KEYS) if v is not None), None)
            monthly = next((v for v in (_num(res.get(k)) for k in _MONTHLY_KEYS) if v is not None), None)
            if hourly is None and monthly is not None:
                hourly = round(monthly / 730, 4)
            if monthly is None and hourly is not None:
                monthly = round(hourly * 730, 2)
            if hourly is not None:
                why = "forward result (approved estimate)"
        it = {"node_id": node.id, "capability": node.capability, "resource": ids,
              "estimated_hourly_usd": hourly, "estimated_monthly_usd": monthly,
              "cost_known": hourly is not None, "cost_source": why, "possibly_provisioned": failed,
              "action_to_stop_cost": node.undo or "",
              "note": "teardown is a separate, explicitly authorized action"}
        it["line"] = _cost_line(it)
        items.append(it)
    if not items:
        return {}
    known = [i["estimated_hourly_usd"] for i in items if i["cost_known"]]
    return {"resources": items,
            "estimated_hourly_usd": round(sum(known), 4) if known else None,
            "estimated_monthly_usd": round(sum(i["estimated_monthly_usd"] for i in items if i["cost_known"]), 2)
            if known else None,
            "unknown_cost_resources": [i["node_id"] for i in items if not i["cost_known"]],
            "complete": all(i["cost_known"] for i in items),
            "summary": "; ".join(i["line"] for i in items)}


def _recovery_actions(plan, incomplete: list[dict], retained: list[dict]) -> list[dict]:
    """What a human/operator must decide next. Never a rollback: reversing a preserved effect is a NEW decision."""
    acts = [{"action": "start_new_mission_for_remaining_steps", "steps": [i["node_id"] for i in incomplete],
             "capabilities": [i.get("capability") for i in incomplete],
             "detail": "a FAILED mission is terminal and cannot be resumed: start a NEW mission covering only these "
                       "remaining steps (the completed steps listed in completed_steps are already in place)"}]
    reversible = [r["node_id"] for r in retained if r.get("reason") == "left_by_policy"]
    if reversible:
        acts.append({"action": "decide_explicit_compensation", "steps": reversible,
                     "detail": "retained by policy; undoing them needs an explicit failure_policy opt-in "
                               "(or a separate forward action) plus its own authorization"})
    other = [r["node_id"] for r in retained if r.get("reason") in ("irreversible", "undeclared_reversibility",
                                                                  "preserved_by_declaration")]
    if other:
        acts.append({"action": "manual_remediation_if_unwanted", "steps": other,
                     "detail": "not reversible by the system, or declared PRESERVE"})
    return acts


def _record_retained(rt, m, outcome: dict) -> None:
    """Append the retained-effects record to the durable log (execution history) when it CHANGED, so the
    history names every effect a failed workflow left behind and why, once per distinct state."""
    retained = outcome.get("retained_effects") or []
    if not retained and not outcome.get("retained_costs"):
        return
    last = next((e.payload for e in reversed(rt.store.for_mission(m.id)) if e.type == C.EV_RETAINED), None)
    if last is not None and last.get("retained_effects") == retained and \
            last.get("incomplete_steps") == outcome.get("incomplete_steps") and \
            last.get("retained_costs") == (outcome.get("retained_costs") or {}):
        return
    rt.store.append(C.EV_RETAINED, m.id, {
        "partially_completed": bool(outcome.get("partially_completed")), "retained_effects": retained,
        "incomplete_steps": outcome.get("incomplete_steps") or [], "failure_policy": outcome.get("failure_policy"),
        "completed_steps": outcome.get("completed_steps") or [], "recovery_actions": outcome.get("recovery_actions") or [],
        "retained_costs": outcome.get("retained_costs") or {}})


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
            cleanup = bool(p.get("cleanup"))
            items.append({**base, "kind": "compensation_approval",
                          "title": (f"Approve CLEANUP of temporary resource {nid}" if cleanup
                                    else f"Approve compensation of {nid}"),
                          "why": (f"CLEANUP of a temporary resource only (other effects are preserved): " if cleanup else "")
                                 + f"run '{v.get('undo')}' (attempt {v['attempt']}) with exactly these inputs",
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
    costs = dict(out.get("retained_costs") or {})
    if m is not None and m.state.value == "failed" and (left or actionable or costs) and not out.get("compensation_acknowledged"):
        items.append({"mission_id": mission_id, "goal": goal, "kind": "effects_left_in_place",
                      "title": f"Mission partially completed; {len(left) or len(actionable) or len(costs.get('resources') or [])} effect(s) were left in place",
                      "why": f"failure policy {out.get('failure_policy')}: succeeded effects are kept, not reversed",
                      "not_compensated": left, "not_reversible": list(out.get("not_reversible") or []),
                      "retained_effects": list(out.get("retained_effects") or []),
                      "incomplete_steps": list(out.get("incomplete_steps") or []),
                      "completed_steps": list(out.get("completed_steps") or []),
                      "recovery_actions": list(out.get("recovery_actions") or []),
                      "retained_costs": costs,
                      "cost_summary": costs.get("summary") or "no cost-bearing resource among the retained effects",
                      **({"legacy_policy_notice": out["legacy_policy_notice"]} if out.get("legacy_policy_notice") else {}),
                      **({"default_policy_warning": out["default_policy_warning"]} if out.get("default_policy_warning") else {}),
                      "decisions": ["manual_remediation", "escalate"]})
    return items
