"""Read-only reconstruction of a mission's receipt from the durable ledger ALONE.

Input: the mission event log (JSONL). No runtime object, no registry, no planner, no model, no connector, no operation-ledger
database and no network: ``reconstruct`` runs inside ``network_disabled()`` and never imports an adapter. It verifies the hash chain,
then folds the events into one receipt: the context pack, the plan and its hash, the approval, the grants and their revocation,
and for each step its operation id, application-native ids, deep link (with the Chatwoot no-per-message-URL note), the expectation
fixed at plan time, the verification, any compensation with its read-back and residual effects, and the classification.

Classification (per step and per mission):
  reversible             applied and independently verified; a declared, read-back-verified compensation exists; not compensated
  compensated            the compensation ran and its read-back showed the effect gone; ``residual`` lists what remains regardless
  irreversible-uncertain the outcome is unknown, or the step was not verified, or its compensation failed / was not verified / was refused
"""
from __future__ import annotations

from agentic_os.mission import compensation as C
from agentic_os.mission.store import EventStore, MissionRepository, verify_chain

from .capabilities import RESIDUAL
from .context import CAPABILITY, IDEMPOTENCY_NOTE, step_of
from .http import network_disabled

SCHEMA = "aosdd.receipt/1"
REVERSIBLE, COMPENSATED, UNCERTAIN, NONE = "reversible", "compensated", "irreversible-uncertain", "none"


def _last(events, type_, **match):
    out = None
    for e in events:
        if e.type == type_ and all(e.payload.get(k) == v for k, v in match.items()):
            out = e
    return out


def reconstruct(source, mission_id: str) -> dict:
    with network_disabled():
        store = source if hasattr(source, "for_mission") else EventStore(source, strict=False)       # read-only: a log that fails its anchor is REPORTED, never written
        return _fold(store, mission_id)


def _fold(store, mid: str) -> dict:
    ev = store.for_mission(mid)
    if not ev:
        raise KeyError(f"no events for mission {mid}")
    chain = verify_chain(ev)
    anchor = store.check_integrity(mid) if hasattr(store, "check_integrity") else None
    whole = verify_chain(store.all())
    repo = MissionRepository(store)
    status = repo.node_status(mid)
    comp = C.fold_compensation(ev)
    opened = _last(ev, "AosddCaseOpened")
    ctx_e = _last(ev, "NodeSucceeded", capability="aosdd.context.read")
    plan_e = _last(ev, "NodeSucceeded", capability="aosdd.plan.build")
    plan = (plan_e.payload.get("result") or {}).get("plan") if plan_e else None
    appr = _last(ev, "AosddPlanApproved")
    rec = {
        "schema": SCHEMA, "mission_id": mid, "state": (repo.state(mid).value if repo.state(mid) else None),
        "case": (opened.payload if opened else None), "goal": repo.goal(mid),
        "ledger": {"events": len(ev), "head": ev[-1].hash, "mission_chain": {"ok": chain.ok, "checked": chain.checked, "unchained": chain.unchained, "reason": chain.reason},
                   "ledger_anchor": (None if anchor is None else {"ok": anchor.ok, "status": anchor.status, "reason": anchor.reason, "records": anchor.count, "anchored": anchor.anchored,
                                                       "protects_against": "a log cut off at the end, or rewritten behind the anchor, by a writer who cannot also rewrite the anchor",
                                                       "does_not_protect_against": "a writer who can rewrite the log and its anchor consistently (no HMAC key is required or assumed unless AGENTIC_OS_ANCHOR_KEY is set)"}),
                   "store_chain": {"ok": whole.ok, "checked": whole.checked, "unchained": whole.unchained, "reason": whole.reason}},
        "context_pack": ((ctx_e.payload.get("result") or {}).get("context_pack") if ctx_e else None),
        "plan": plan, "plan_sha256": (plan or {}).get("plan_sha256"),
        "approval": ({"approver": appr.payload["claims"]["approver"], "plan_sha256": appr.payload["claims"]["plan_sha256"], "steps": appr.payload["claims"]["steps"],
                      "expires_at": appr.payload["claims"]["exp"], "signature_present": bool(appr.payload.get("sig"))} if appr else None),
        "grants": [dict(e.payload, seq=e.seq, type=e.type) for e in ev if e.type in ("AosddGrantIssued", "AosddGrantRevoked")],
        "authorizations": [{"seq": e.seq, "type": e.type, "capability": e.payload.get("capability"), "grant_index": e.payload.get("grant_index"),
                            "rule": e.payload.get("rule"), "envelope_digest": e.payload.get("envelope_digest")}
                           for e in ev if e.type in ("ScopeAuthorized", "AuthorityDeclined")],
        "halts": [dict(e.payload, seq=e.seq, type=e.type) for e in ev if e.type in ("AosddMissionHalted", "AosddStepRefused", "AosddApprovalRefused", "ExecutionAuthorityRefused", "AuthorityDeclined", "PrincipalRefused")],
        "duplicates": [dict(e.payload, seq=e.seq, type=e.type) for e in ev if e.type in ("AosddDuplicateSubmit", "AosddOpReplayed")],
        "resumes": [dict(e.payload, seq=e.seq, type=e.type) for e in ev if e.type in ("AosddResumeRequested", "AosddReconciled", "AosddCompensationRequested")],
        "steps": [], "mission_classification": NONE, "residual_effects": [],
    }
    if plan:
        for ps in plan["steps"]:
            rec["steps"].append(_step(ev, ps, plan, status, comp))
        classes = [s["classification"] for s in rec["steps"] if s["attempted"] and s["classification"] != NONE]
        if UNCERTAIN in classes:
            rec["mission_classification"] = UNCERTAIN
        elif classes and all(c == COMPENSATED for c in classes):
            rec["mission_classification"] = COMPENSATED
        elif classes and any(c == COMPENSATED for c in classes):
            rec["mission_classification"] = "mixed: " + ", ".join(f"{s['step']}={s['classification']}" for s in rec["steps"] if s["attempted"])
        elif classes:
            rec["mission_classification"] = REVERSIBLE
        rec["residual_effects"] = [{"step": s["step"], "residual": s["residual"]} for s in rec["steps"] if s["classification"] == COMPENSATED and s["residual"]]
    return rec


def _step(ev, ps: dict, plan: dict, status: dict, comp: dict) -> dict:
    op = ps["op_id"]
    mine = [e for e in ev if e.payload.get("op_id") == op]
    t = lambda name: [e for e in mine if e.type == name]   # noqa: E731
    node_id = next((e.payload["node_id"] for e in ev if e.type == "NodeDispatched" and e.payload.get("capability") == ps["capability"]), None)
    applied, intents = t("AosddOpApplied"), t("AosddOpIntent")
    unknown, refused = t("AosddOpUnknown"), t("AosddOpRefused")
    ver = t("AosddStepVerified")
    adopted_rec = t("AosddReconciled")
    undo, undo_ver = t("AosddUndoResult"), t("AosddUndoVerified")
    c = comp.get(node_id or "", {})
    v_last = ver[-1].payload if ver else None
    a = applied[-1].payload if applied else None
    out = {"step": ps["step"], "app": ps["app"], "capability": ps["capability"], "op_id": op, "node_id": node_id,
           "node_state": getattr(status.get(node_id), "value", None) if node_id else None,
           "attempted": bool(intents), "request_persisted_before_send": bool(intents) and (not applied or intents[0].seq < applied[0].seq),
           "target": ps["target"], "expect": ps["expect"], "native_id": a["native_id"] if a else None, "native_ids": a["native_ids"] if a else None,
           "ui_url": a["ui_url"] if a else None, "ui_url_note": a.get("ui_url_note", "") if a else "", "created": a["created"] if a else None,
           "adopted": (a["adopted"] if a else None) or bool([r for r in adopted_rec if r.payload.get("found")]),
           "outcome": ("applied" if a else "unknown" if unknown else "refused" if refused else "not_attempted"),
           "error": (refused[-1].payload if refused else unknown[-1].payload if unknown else None),
           "verification": ({"status": v_last["status"], "method": v_last["method"], "reader": v_last["reader"], "checks": v_last["checks"]} if v_last else None),
           "compensation": ({"status": c.get("status"), "attempts": c.get("attempts"), "undo_result": undo[-1].payload if undo else None,
                              "readback": ({"status": undo_ver[-1].payload["status"], "checks": undo_ver[-1].payload["checks"]} if undo_ver else None)} if c else None),
           "residual": [], "declared_residual": RESIDUAL[ps["step"]],
           "idempotency": ps.get("idempotency"), "idempotency_note": IDEMPOTENCY_NOTE.get(ps["app"], "")}
    if undo_ver:
        out["residual"] = undo_ver[-1].payload.get("residual") or []
    if not out["attempted"]:
        out["classification"] = NONE
    elif out["outcome"] in ("unknown",) or (out["outcome"] == "applied" and (not v_last or v_last["status"] != "VERIFIED") and not (c and c.get("status") == "compensated")):
        out["classification"] = UNCERTAIN
    elif c and c.get("status") == "compensated":
        out["classification"] = COMPENSATED
    elif c and c.get("status") in ("failed", "executed_unverified", "skipped", "approval_required", "acknowledged"):
        out["classification"] = UNCERTAIN
    elif out["outcome"] == "applied":
        out["classification"] = REVERSIBLE
    elif out["outcome"] == "refused" and (out["error"] or {}).get("residual"):
        out["classification"] = COMPENSATED     # the adapter took back its own partial write; the residual row is recorded
        out["residual"] = [{"what": "partial write taken back inside the step", **out["error"]["residual"]}]
    else:
        out["classification"] = NONE            # refused: the application confirmed nothing took effect
    return out
