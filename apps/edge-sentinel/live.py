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
from typing import Callable

from . import core
from .case_store import CaseStore
from .evidence import ActionReceipt, ActionRequest, ActionState, CaseStatus, SecurityDecision
from .incident import ingest_crowdsec_alert, record_response, replay_case


class ExecutionError(Exception):
    """Raised when execution is requested for an action that has not been approved."""

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
    """Test hook: drop the process store."""
    global _STORE
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
    return ids


def _open_action(st: CaseStore, case) -> dict | None:
    """The action still in the operator's hands: AWAITING_APPROVAL (needs a demo approve) or APPROVED
    (approved, awaiting the authenticated execution step). Drives which buttons the case panel shows."""
    for rid in case.action_refs:
        a = st.actions.get(rid)
        if a is not None and a.state in (ActionState.AWAITING_APPROVAL, ActionState.APPROVED):
            return {"request_id": rid, "capability": a.capability,
                    "parameters": dict(a.parameters), "state": a.state.value}
    return None


def _approving_decision_id(st: CaseStore, case, request_id: str) -> str:
    """The id of the most recent APPROVED decision on this request — for receipt linkage at execution time."""
    for did in reversed(case.decision_refs):
        d = st.decisions.get(did)
        if d is not None and d.request_id == request_id and d.approved:
            return did
    return ""


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


def decide(case_id: str, request_id: str, *, approved: bool, actor: str,
           st: CaseStore | None = None) -> dict:
    """The DEMO-FACING decision step — OPEN (no authentication) and with NO edge effect.

    Reject records the human rejection and stops. Approve records the approval and advances the action to
    APPROVED (staged) — it does NOT touch the edge. The real kick-off is a SEPARATE, AUTHENTICATED step
    (:func:`execute`), mirroring the Growth/Partners 'approve in the demo, execute for real behind auth'
    split. Raises KeyError for an unknown case/request (→ 404)."""
    st = st or store()
    case = st.cases[case_id]       # KeyError → 404
    req = st.actions[request_id]   # KeyError → 404

    if not approved:
        decision, _ = record_response(st, case, request_id, actor=actor, approved=False)
        return {"approved": False, "awaiting_execution": False, "decision": decision.to_dict(),
                "case_status": case.status.value}

    # stage the approval: decision recorded + action advanced to APPROVED, but nothing executed and the edge
    # is untouched. Execution requires the authenticated execute() step.
    decision = st.put_decision(SecurityDecision(request_id=request_id, actor=actor, approved=True,
                                                rationale="approved (staged for authenticated execution)"))
    case.decision_refs = tuple(dict.fromkeys(case.decision_refs + (decision.decision_id,)))
    st.put_action(ActionRequest(capability=req.capability, parameters=req.parameters,
                                finding_refs=req.finding_refs, evidence_refs=req.evidence_refs,
                                approval_required=req.approval_required, state=ActionState.APPROVED))
    st.put_case(case)
    return {"approved": True, "awaiting_execution": True, "decision": decision.to_dict(),
            "case_status": case.status.value}


def execute(case_id: str, request_id: str, *, actor: str, st: CaseStore | None = None,
            operator=None, enabled: bool | None = None) -> dict:
    """The AUTHENTICATED execution step — the real kick-off. Call this ONLY from the auth-gated endpoint.

    Preconditions: the action must already be APPROVED (ExecutionError otherwise). Dry-run unless
    SENTINEL_BLOCK_ENABLED (so the public demo never mutates the live edge by default) — a dry-run records a
    DRY_RUN receipt and changes nothing. When enabled, it executes through the REAL Mission Runtime operator
    (propose→execute→verify); if the kernel (agentic_os) is absent it falls back to the direct CrowdSec core
    effect. Either way it records a receipt (execution proof) and a DISTINCT verification, and moves the case
    to CONTAINED on success. Raises KeyError for an unknown case/request (→ 404)."""
    st = st or store()
    case = st.cases[case_id]       # KeyError → 404
    req = st.actions[request_id]   # KeyError → 404
    if req.state != ActionState.APPROVED:
        raise ExecutionError(f"action {request_id} is {req.state.value}, not APPROVED — approve it first")

    if enabled is None:
        from .auth import block_enabled
        enabled = block_enabled()
    decision_id = _approving_decision_id(st, case, request_id)

    if not enabled:
        receipt = st.put_receipt(ActionReceipt(
            request_id=request_id, decision_id=decision_id, status="DRY_RUN", external_ref="dry-run",
            error="SENTINEL_BLOCK_ENABLED not set — edge not touched"))
        return {"executed": False, "dry_run": True, "receipt": receipt.to_dict(),
                "verification": None, "case_status": case.status.value}

    ext = err = vdetail = ""
    verified = False
    op = None
    try:
        from .response import execute_response, propose_response, verify_response
        op = operator or _operator()
    except Exception:  # noqa: BLE001 — kernel (agentic_os) absent → direct core effect below
        op = None

    if op is not None:
        proposal = propose_response(op, req, case_id=case_id)  # governed-response invariant
        dec = SecurityDecision(request_id=request_id, actor=actor, approved=True, rationale="operator execute")
        rr = execute_response(op, proposal, decision=dec)      # the REAL ban via the operator
        ext, err = rr.external_ref, rr.error
        ver = verify_response(op, proposal, rr)                # distinct read-only re-check
        verified, vdetail = ver.verified, ver.detail
    else:
        try:
            result = core.block_ip(dict(req.parameters)) or {}
            if result.get("status") == "error":
                err = result.get("error", "block failed")
            else:
                ext = str(result.get("id") or result.get("decision_id") or "")
            ip = req.parameters.get("ip", "")
            posture = core.triage() if not err else {}
            verified = bool(ip) and ip in repr(posture)
            vdetail = ("ban present" if verified else "ban not observed") + f" for {ip}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"

    status = "SUCCEEDED" if not err else "FAILED"
    receipt = st.put_receipt(ActionReceipt(request_id=request_id, decision_id=decision_id, status=status,
                                           external_ref=ext, error=err))
    st.put_action(ActionRequest(capability=req.capability, parameters=req.parameters,
                                finding_refs=req.finding_refs, evidence_refs=req.evidence_refs,
                                approval_required=req.approval_required,
                                state=ActionState.EXECUTED if status == "SUCCEEDED" else ActionState.FAILED))
    if status == "SUCCEEDED":
        case.status = CaseStatus.CONTAINED
    st.put_case(case)
    return {"executed": status == "SUCCEEDED", "dry_run": False, "receipt": receipt.to_dict(),
            "verification": {"verified": verified, "detail": vdetail}, "case_status": case.status.value}


def _operator():
    from .operator import build_edge_sentinel_operator
    return build_edge_sentinel_operator()
