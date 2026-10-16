"""Live wiring — connect the running SOC app (World A) to the Security Intelligence Core (World B).

Until now the typed case spine (evidence → observation → finding → approval-gated action → decision →
receipt → replay) existed and was unit-tested, but nothing on the HTTP surface ever built a case from a
real alert. This module is the seam that makes it real:

  * :func:`sync_cases` pulls the SAME live CrowdSec alerts the dashboard already reads (``core.fetch_alerts``)
    and ingests each into a process-wide :class:`CaseStore` via the tested :func:`incident.ingest_crowdsec_alert`.
    Ingestion is content-addressed and idempotent, so re-syncing never duplicates a case.
  * :func:`case_summaries` / :func:`case_detail` expose the real cases (never a canned scenario); detail
    re-proves the replay invariant from the stored immutable evidence.
  * :func:`respond` runs the governed response loop through the REAL Mission Runtime operator
    (``response.propose_response`` / ``execute_response`` / ``verify_response``) and records the trail with the
    tested :func:`incident.record_response`. Governance is the gate: a block only executes on an approved
    decision, the receipt is distinct from verification, and a rejection never touches the edge.

The operator/response path is imported lazily so the module stays importable (sync/summaries/detail still
work) even when ``agentic_os`` is not installed — the same standalone guarantee the app already makes.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from . import core
from . import statefile
from .case_store import CaseStore
from .evidence import ActionReceipt, ActionRequest, ActionState, CaseStatus, SecurityDecision
from .incident import ingest_crowdsec_alert, replay_case


class ExecutionError(Exception):
    """Raised when a decision or execution is requested for an action in the wrong state."""


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

_STORE: CaseStore | None = None


def store() -> CaseStore:
    """The process-wide case store. In-memory + append-only today; a durable Mission Event Store backend
    slots in behind the same interface later (see case_store docstring)."""
    global _STORE
    if _STORE is None:
        _STORE = CaseStore()
    return _STORE


def reset_store() -> CaseStore:
    """Test hook: drop the process store (and in-memory attempt counters)."""
    global _STORE
    _ATTEMPTS.clear()
    _INFLIGHT.clear()
    _STORE = CaseStore()
    return _STORE


def sync_cases(st: CaseStore | None = None, *, alerts_fn: Callable[[], list[dict]] | None = None) -> list[str]:
    """Ingest the current live CrowdSec alerts into cases. Idempotent: returns the case ids touched.
    Degrades to [] (never raises, never fabricates) when the core is unreachable."""
    st = st or store()
    fn = alerts_fn or core.fetch_alerts
    try:
        alerts = fn() or []
    except Exception:  # noqa: BLE001 — offline core → no cases, not a crash
        alerts = []
    ids: list[str] = []
    for alert in alerts:
        try:
            case = ingest_crowdsec_alert(st, alert)
            ids.append(case.case_id)
        except Exception:  # noqa: BLE001 — one malformed alert never sinks the sync
            continue
    try:
        _apply_state(st)
    except statefile.StateUnavailable:
        pass                    # decide/execute re-check and refuse; listing still works
    return ids


def _open_action(st: CaseStore, case) -> dict | None:
    """The action still in the operator's hands: AWAITING_APPROVAL (needs a demo approve) or APPROVED
    (approved, awaiting the authenticated execution step). Drives which buttons the case panel shows."""
    for rid in case.action_refs:
        a = st.actions.get(rid)
        if a is not None and a.state in (ActionState.AWAITING_APPROVAL, ActionState.APPROVED, ActionState.FAILED):
            return {"request_id": rid, "capability": a.capability,
                    "parameters": dict(a.parameters), "state": a.state.value}
    return None


def state_health() -> dict:
    """For /health: is the durable case state usable?"""
    return statefile.health()


def case_summaries(st: CaseStore | None = None) -> list[dict]:
    """One row per real case, worst severity first. Each row is a replayable SecurityCase, not a literal."""
    st = st or store()
    rows: list[dict] = []
    for c in st.cases.values():
        rows.append({
            "case_id": c.case_id,
            "case_type": c.case_type.value,
            "severity": c.severity.value,
            "status": c.status.value,
            "title": c.title,
            "created_at": c.created_at,
            "counts": {
                "evidence": len(c.evidence_refs),
                "findings": len(c.finding_refs),
                "actions": len(c.action_refs),
                "decisions": len(c.decision_refs),
            },
            "open_action": _open_action(st, c),
        })
    rows.sort(key=lambda r: (_SEVERITY_ORDER.get(r["severity"], 9), r["created_at"]), reverse=False)
    rows.sort(key=lambda r: _SEVERITY_ORDER.get(r["severity"], 9))
    return rows


def _replay_proof(st: CaseStore, case) -> dict:
    """Re-prove the replay invariant: re-ingest the case's own stored raw evidence into a fresh store and
    confirm the content-addressed case id is identical. The narrative can change; the evidence-derived id can't."""
    for eid in case.evidence_refs:
        ev = st.evidence.get(eid)
        if ev is None or ev.kind != "crowdsec_alert":
            continue
        raw = st.get_raw(ev)
        if raw is None:
            continue
        try:
            alert = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"deterministic": False, "detail": "raw evidence is not replayable JSON"}
        proof = replay_case(st, alert)
        return {"deterministic": proof["case_id"] == case.case_id, "case_id": proof["case_id"]}
    return {"deterministic": False, "detail": "no replayable crowdsec_alert evidence on this case"}


def case_detail(case_id: str, st: CaseStore | None = None) -> dict | None:
    """The full resolved bundle a case/evidence view reads, plus the replay proof."""
    st = st or store()
    if case_id not in st.cases:
        return None
    bundle = st.case_bundle(case_id)
    bundle["replay"] = _replay_proof(st, st.cases[case_id])
    return bundle


_LOCK = threading.RLock()
_INFLIGHT: set = set()                    # request ids whose ban is executing right now (the claim)
_ATTEMPTS: dict = {}                      # request_id -> {"n": attempts, "last": epoch seconds}
_NOW = time.time                          # injectable clock (tests)
MAX_ATTEMPTS = 5


def _resolve(st: CaseStore, case_id: str, request_id: str):
    """Case + action for a request, requiring that the case actually owns the action (KeyError -> 404)."""
    case = st.cases[case_id]
    if request_id not in case.action_refs:
        raise KeyError(request_id)
    return case, st.actions[request_id]


def _set_state(st: CaseStore, req: ActionRequest, state: ActionState) -> None:
    st.put_action(ActionRequest(capability=req.capability, parameters=req.parameters,
                                finding_refs=req.finding_refs, evidence_refs=req.evidence_refs,
                                approval_required=req.approval_required, created_at=req.created_at, state=state))


def _persist(st: CaseStore) -> None:
    """Write every action that has left AWAITING_APPROVAL (state, attempts) with its decisions, receipts and the
    owning cases' status to the state file, merged over what is stored. Raises StateUnavailable."""
    if not statefile.directory():
        return
    data = statefile.load() or statefile.empty()
    for rid, a in st.actions.items():
        if a.state != ActionState.AWAITING_APPROVAL:
            data["actions"][rid] = {"state": a.state.value, "attempts": dict(_ATTEMPTS.get(rid, {"n": 0, "last": 0}))}
    for cid, c in st.cases.items():
        if any(r in data["actions"] for r in c.action_refs):
            data["cases"][cid] = {"status": c.status.value, "decision_refs": list(c.decision_refs)}
            for did in c.decision_refs:
                d = st.decisions.get(did)
                if d is not None:
                    data["decisions"][did] = {"request_id": d.request_id, "actor": d.actor,
                                              "approved": d.approved, "rationale": d.rationale, "at": d.at}
    for r in st.receipts.values():
        if r.request_id in data["actions"]:
            data["receipts"][r.receipt_id] = {"request_id": r.request_id, "decision_id": r.decision_id,
                                              "status": r.status, "external_ref": r.external_ref,
                                              "error": r.error, "at": r.at}
    statefile.save(data)


def _apply_state(st: CaseStore) -> None:
    """Overlay the stored state onto freshly ingested cases so a restart keeps terminal states. Raises
    StateUnavailable when the file is unreadable."""
    data = statefile.load()
    if not data:
        return
    for did, d in data["decisions"].items():
        if did not in st.decisions:
            st.decisions[did] = SecurityDecision(request_id=d["request_id"], actor=d["actor"],
                                                 approved=bool(d["approved"]), rationale=d.get("rationale", ""),
                                                 at=d.get("at", ""))
    for rid, meta in data["actions"].items():
        a = st.actions.get(rid)
        if a is None:
            continue
        want = ActionState(meta["state"])
        if a.state != want:
            _set_state(st, a, want)
        _ATTEMPTS[rid] = dict(meta.get("attempts") or {"n": 0, "last": 0})
    for cid, meta in data["cases"].items():
        c = st.cases.get(cid)
        if c is not None:
            c.status = CaseStatus(meta["status"])
            c.decision_refs = tuple(meta["decision_refs"])
    for rcid, r in data["receipts"].items():
        if r.get("request_id") in st.actions and rcid not in st.receipts:
            st.receipts[rcid] = ActionReceipt(request_id=r["request_id"], decision_id=r.get("decision_id", ""),
                                              status=r.get("status", ""), external_ref=r.get("external_ref", ""),
                                              error=r.get("error", ""), at=r.get("at", ""))


def _check_state_ok() -> None:
    """Refuse to decide or execute when the configured state file is unreadable."""
    try:
        statefile.load()
    except statefile.StateUnavailable as e:
        raise ExecutionError(f"case state store unavailable ({e}); refusing") from None


def _exhausted(rid: str) -> bool:
    return _ATTEMPTS.get(rid, {"n": 0})["n"] >= MAX_ATTEMPTS


def _check_decidable(req: ActionRequest, approved: bool) -> None:
    if req.request_id in _INFLIGHT:
        raise ExecutionError(f"action {req.request_id} is executing right now; it cannot be changed")
    s = req.state
    ok = (s == ActionState.AWAITING_APPROVAL
          or (not approved and s == ActionState.APPROVED)                       # withdraw a staged approval
          or (s == ActionState.FAILED and _exhausted(req.request_id)))          # retries used up: re-approve or give up
    if not ok:
        raise ExecutionError(f"action {req.request_id} is {s.value}; a decision is accepted only from "
                             "AWAITING_APPROVAL (reject also from APPROVED, either after exhausted retries)")


def preview_decide(case_id: str, request_id: str, *, approved: bool, st: CaseStore | None = None) -> dict:
    """What an unauthenticated visitor gets: the same validation as a real decision, and a SIMULATED result.
    Nothing is recorded or persisted."""
    st = st or store()
    with _LOCK:
        case, req = _resolve(st, case_id, request_id)
        _check_decidable(req, approved)
        return {"approved": bool(approved), "awaiting_execution": False, "simulated": True,
                "case_status": case.status.value,
                "note": "simulated: sign in as the operator to record a real decision"}


def decide(case_id: str, request_id: str, *, approved: bool, actor: str, st: CaseStore | None = None) -> dict:
    """Record a real decision (the HTTP layer calls this only for an authenticated operator). NO edge effect.

    The request must belong to the case. Approve is accepted from AWAITING_APPROVAL (or FAILED once retries are
    exhausted, which resets the attempt budget); reject also withdraws a staged APPROVED action. EXECUTED and
    REJECTED are final, and an action that is executing right now cannot be changed. Reject -> REJECTED,
    approve -> APPROVED (execution is a separate step). State is persisted first-class: if it cannot be saved
    the decision is rolled back. KeyError -> 404, ExecutionError -> 409."""
    st = st or store()
    with _LOCK:
        case, req = _resolve(st, case_id, request_id)
        _check_decidable(req, approved)
        _check_state_ok()
        actor = (actor or "").strip() or "anonymous"
        prev = (req, case.status, case.decision_refs, dict(_ATTEMPTS.get(request_id, {"n": 0, "last": 0})))
        withdrawn = req.state == ActionState.APPROVED
        if req.state == ActionState.FAILED:
            _ATTEMPTS[request_id] = {"n": 0, "last": 0}
        decision = st.put_decision(SecurityDecision(
            request_id=request_id, actor=actor, approved=bool(approved),
            rationale=("approved (staged for authenticated execution)" if approved
                       else ("withdrawn" if withdrawn else "rejected"))))
        case.decision_refs = tuple(dict.fromkeys(case.decision_refs + (decision.decision_id,)))
        _set_state(st, req, ActionState.APPROVED if approved else ActionState.REJECTED)
        if not approved:
            case.status = CaseStatus.INVESTIGATING
        st.put_case(case)
        try:
            _persist(st)
        except statefile.StateUnavailable as e:        # memory must never run ahead of the durable record
            st.actions[request_id] = prev[0]
            case.status, case.decision_refs = prev[1], prev[2]
            st.decisions.pop(decision.decision_id, None)
            _ATTEMPTS[request_id] = prev[3]
            raise ExecutionError(f"decision not recorded: state could not be saved ({e})") from None
        return {"approved": bool(approved), "awaiting_execution": bool(approved), "decision": decision.to_dict(),
                "case_status": case.status.value}


def _approving_decision(st: CaseStore, case, request_id: str):
    """The newest recorded approved decision for exactly this request on this case (None if never approved)."""
    for did in reversed(case.decision_refs):
        d = st.decisions.get(did)
        if d is not None and d.approved and d.request_id == request_id:
            return d
    return None


def execute(case_id: str, request_id: str, *, actor: str, st: CaseStore | None = None,
            operator=None, enabled: bool | None = None) -> dict:
    """The AUTHENTICATED execution step. Call only from the auth-gated endpoint.

    Allowed from APPROVED, or from FAILED (a retry: at most MAX_ATTEMPTS, exponential backoff of 5s doubling to a
    300s cap), and only with a recorded approving decision on this case. EXECUTED is final. The check and the
    claim happen under one lock, so concurrent calls run the ban once. The recorded decision is what the governed
    response runs against. Dry-run unless SENTINEL_BLOCK_ENABLED. Before the edge is touched the attempt is saved
    (as FAILED, i.e. retryable); if that fails nothing is executed."""
    st = st or store()
    if enabled is None:
        from .auth import block_enabled
        enabled = block_enabled()
    with _LOCK:
        case, req = _resolve(st, case_id, request_id)
        if req.state not in (ActionState.APPROVED, ActionState.FAILED):
            raise ExecutionError(f"action {request_id} is {req.state.value}, not APPROVED - approve it first")
        _check_state_ok()
        recorded = _approving_decision(st, case, request_id)
        if recorded is None:
            raise ExecutionError(f"action {request_id} has no recorded approving decision on this case")
        if request_id in _INFLIGHT:
            raise ExecutionError(f"action {request_id} is already executing")
        if enabled and req.state == ActionState.FAILED:
            att = _ATTEMPTS.get(request_id, {"n": 0, "last": 0})
            if att["n"] >= MAX_ATTEMPTS:
                raise ExecutionError(f"action {request_id} failed {att['n']} times; an operator re-approval is needed")
            wait = min(300.0, 5.0 * 2 ** max(0, att["n"] - 1)) - (_NOW() - att["last"])
            if wait > 0:
                raise ExecutionError(f"action {request_id} failed recently; retry in {int(wait) + 1}s")
        if not enabled:
            receipt = st.put_receipt(ActionReceipt(
                request_id=request_id, decision_id=recorded.decision_id, status="DRY_RUN", external_ref="dry-run",
                error="SENTINEL_BLOCK_ENABLED not set — edge not touched"))
            return {"executed": False, "dry_run": True, "receipt": receipt.to_dict(),
                    "verification": None, "case_status": case.status.value}
        _INFLIGHT.add(request_id)
    try:
        return _execute_claimed(st, case, req, recorded, operator=operator)
    finally:
        with _LOCK:
            _INFLIGHT.discard(request_id)


def _execute_claimed(st: CaseStore, case, req: ActionRequest, recorded, *, operator) -> dict:
    request_id, case_id = req.request_id, case.case_id
    att = _ATTEMPTS.setdefault(request_id, {"n": 0, "last": 0})
    prev_att, prev_req = dict(att), req
    with _LOCK:
        att["n"] += 1
        att["last"] = _NOW()
        _set_state(st, req, ActionState.FAILED)
        try:
            _persist(st)
        except statefile.StateUnavailable as e:
            st.actions[request_id] = prev_req
            _ATTEMPTS[request_id] = prev_att
            raise ExecutionError(f"not executed: state could not be saved ({e})") from None

    ext = err = vdetail = ""
    verified = False
    op = None
    try:
        from .response import execute_response, propose_response, verify_response
        op = operator or _operator()
    except Exception:  # noqa: BLE001 - kernel (agentic_os) absent -> direct core effect below
        op = None
    try:
        if op is not None:
            proposal = propose_response(op, req, case_id=case_id)
            rr = execute_response(op, proposal, decision=recorded)      # the real ban, under the recorded decision
            ext, err = rr.external_ref, rr.error
            if rr.status == "SUCCEEDED":
                ver = verify_response(op, proposal, rr)
                verified, vdetail = ver.verified, ver.detail
        else:
            result = core.block_ip(dict(req.parameters)) or {}
            if result.get("status") == "error":
                err = result.get("error", "block failed")
            else:
                ext = str(result.get("id") or result.get("decision_id") or "")
            verified = (not err) and bool(result.get("ip_blocked"))
            vdetail = ("ban present" if verified else "ban not observed") + f" for {req.parameters.get('ip', '')}"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    status = "SUCCEEDED" if not err else "FAILED"
    with _LOCK:
        receipt = st.put_receipt(ActionReceipt(request_id=request_id, decision_id=recorded.decision_id,
                                               status=status, external_ref=ext, error=err))
        _set_state(st, req, ActionState.EXECUTED if status == "SUCCEEDED" else ActionState.FAILED)
        if status == "SUCCEEDED":
            case.status = CaseStatus.CONTAINED
        st.put_case(case)
        try:
            _persist(st)
        except statefile.StateUnavailable as e:    # the ban happened; say that the record could not be saved
            vdetail = (vdetail + f" [state not persisted: {e}]").strip()
    return {"executed": status == "SUCCEEDED", "dry_run": False, "receipt": receipt.to_dict(),
            "verification": {"verified": verified, "detail": vdetail}, "case_status": case.status.value}


def _operator():
    from .operator import build_edge_sentinel_operator
    return build_edge_sentinel_operator()
