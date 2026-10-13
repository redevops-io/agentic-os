"""Operator-only HTTP surface for the billing closed loop (observe -> decide -> govern -> act -> verify).

    POST /api/decide                          observe Lago, run one decision cycle, return the opportunities,
                                              the selected action and the PARKED mission id. Executes nothing.
    POST /api/decide/{mission_id}/approve     the operator's explicit approval: the runtime then delivers the
                                              approval marker and billing.dunning retries ONLY the chosen
                                              invoices, read back from Lago. Body ``{"decision": "approve|reject"}``.

Both are authored as mission operations (the runtime launches/drives the mission), never as direct core
calls. Auth: HTTP Basic from OPERATOR_BASIC_AUTH_USER/_PASS or ``user:pass`` in OPERATOR_BASIC_AUTH /
OUTREACH_BASIC_AUTH; with none configured every route fails closed (503). Request bodies are stripped of any
caller-supplied approval keys and never carry invoice ids -- scope comes only from the observed state.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from agentic_os.mission.approval import strip_approval_keys

from . import decision, gate, observe

_strip_approval = strip_approval_keys
router = APIRouter()

_LOOP: Optional[decision.ClosedLoop] = None


def get_loop() -> decision.ClosedLoop:
    global _LOOP
    if _LOOP is None:
        _LOOP = decision.build_loop()
    return _LOOP


def set_loop(loop: Optional[decision.ClosedLoop]) -> None:
    global _LOOP
    _LOOP = loop


def require_operator(request: Request) -> None:
    user, pw = gate.operator_credential()
    if not (user and pw):
        raise HTTPException(503, "operator confirmation is not configured (set OPERATOR_BASIC_AUTH_USER/_PASS)")
    if not gate.is_operator(request):
        raise HTTPException(401, "operator authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="agentic-billing operator"'})


async def _body(request: Request) -> dict:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


@router.post("/api/decide")
async def api_decide(request: Request) -> JSONResponse:
    require_operator(request)
    body = await _body(request)
    loop = get_loop()
    now = loop.clock()
    try:
        signals = observe.observe_accounts(loop.ingestor, now=now)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    want = body.get("account")
    if want:
        if want not in signals:
            raise HTTPException(404, f"account {want!r} not found in Lago")
        sig = signals[want]
    elif signals:
        sig = max(signals.values(), key=lambda s: (s.overdue_amount_cents, s.overdue_invoices, s.account))
    else:
        return JSONResponse({"opportunities": [], "selected": None, "mission_id": None,
                             "note": "no accounts observed in Lago"})
    cycle = decision.run_decision_cycle(sig, store=loop.interventions, policy=loop.policies.active,
                                        loop=loop, now=now)
    d = decision.describe_cycle(cycle)
    return JSONResponse({"opportunities": [d["opportunity"]], "selected": d["selected"], "outcome": d["outcome"],
                         "intervention_id": d["intervention_id"], "mission_id": d["mission_id"],
                         "mission_state": d["mission_state"], "inputs": d["inputs"],
                         "observation_ids": list(sig.observation_ids), "policy_version": loop.policies.active.version})


@router.post("/api/decide/{mission_id}/approve")
async def api_decide_approve(mission_id: str, request: Request) -> JSONResponse:
    require_operator(request)
    body = await _body(request)
    verdict = str(body.get("decision", "approve"))
    if verdict not in ("approve", "reject"):
        raise HTTPException(400, "decision must be 'approve' or 'reject'")
    try:
        out = decision.approve_mission(get_loop(), mission_id, decision=verdict, approved_by="operator")
    except KeyError:
        raise HTTPException(404, f"unknown mission {mission_id!r}")
    return JSONResponse(out)
