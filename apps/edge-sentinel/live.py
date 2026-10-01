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
from .evidence import ActionState, CaseStatus
from .incident import ingest_crowdsec_alert, record_response, replay_case

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


def _pending_action(st: CaseStore, case) -> dict | None:
    for rid in case.action_refs:
        a = st.actions.get(rid)
        if a is not None and a.state == ActionState.AWAITING_APPROVAL:
            return {"request_id": rid, "capability": a.capability, "parameters": dict(a.parameters)}
    return None


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
            "pending_action": _pending_action(st, c),
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


def respond(case_id: str, request_id: str, *, approved: bool, actor: str,
            st: CaseStore | None = None, operator=None) -> dict:
    """The governed response. Reject records the human decision and stops — the edge is never touched.
    Approve binds the request to the REAL operator capability, executes only on the approved decision,
    records a receipt (execution proof) and then verifies (a separate read-only check that the ban landed).

    Returns the decision/receipt/verification trail. Raises KeyError for an unknown case/request so the
    HTTP layer can answer 404; GovernanceError (from response.propose_response) surfaces a refusal to
    propose an ungoverned side-effecting action."""
    st = st or store()
    case = st.cases[case_id]       # KeyError → 404
    req = st.actions[request_id]   # KeyError → 404

    if not approved:
        decision, _ = record_response(st, case, request_id, actor=actor, approved=False)
        return {"approved": False, "decision": decision.to_dict(), "receipt": None,
                "verification": None, "case_status": case.status.value}

    # lazy: the governed-execution path needs the Mission Runtime operator SDK
    from .evidence import SecurityDecision
    from .response import execute_response, propose_response, verify_response

    op = operator or _operator()
    proposal = propose_response(op, req, case_id=case_id)  # enforces the governed-response invariant
    decision_obj = SecurityDecision(request_id=request_id, actor=actor, approved=True, rationale="approved")
    receipt_real = execute_response(op, proposal, decision=decision_obj)  # the REAL ban, only now

    # record the trail with the tested helper; same (request_id, actor, approved) ⇒ same decision id,
    # so this does not duplicate the decision — it carries the real operator's external_ref/error onto it.
    decision, receipt = record_response(
        st, case, request_id, actor=actor, approved=True,
        external_ref=receipt_real.external_ref, error=receipt_real.error)

    verification = verify_response(op, proposal, receipt_real)  # distinct from the receipt
    return {"approved": True, "decision": decision.to_dict(),
            "receipt": receipt.to_dict() if receipt else None,
            "verification": verification.to_dict(), "case_status": case.status.value}


def _operator():
    from .operator import build_edge_sentinel_operator
    return build_edge_sentinel_operator()
