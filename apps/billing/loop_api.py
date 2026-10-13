"""Operator-only HTTP surface for the billing closed loop (observe -> decide -> govern -> act -> verify).

    POST /api/decide                          observe Lago, run one decision cycle, return the opportunities,
                                              the selected action and the PARKED mission id. Executes nothing.
    POST /api/decide/{mission_id}/approve     the operator's explicit approval: the runtime then delivers the
                                              approval marker and billing.dunning retries ONLY the chosen
                                              invoices, read back from Lago. Body ``{"decision": "approve|reject"}``.

    POST /api/outcomes/observe                LEARN: attribute payment/no-payment to executed dunning interventions
                                              older than ``after_days`` (default 7), once each. Idempotent.
    POST /api/policy/refit                    refit the utility model -- ONLY through policy_promotion's held-out
                                              gate (``{"apply": true}`` to promote when the gate passes).

Both are authored as mission operations (the runtime launches/drives the mission), never as direct core
calls. Auth: HTTP Basic from OPERATOR_BASIC_AUTH_USER/_PASS or ``user:pass`` in OPERATOR_BASIC_AUTH /
OUTREACH_BASIC_AUTH; with none configured every route fails closed (503). Request bodies are stripped of any
caller-supplied approval keys and never carry invoice ids -- scope comes only from the observed state.
"""
from __future__ import annotations

import logging
import os
import threading
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


def run_observe(loop: decision.ClosedLoop, *, after_days: float = 7.0) -> list:
    """Observe outcomes for executed dunning older than ``after_days`` (single-flight under ``loop.lock``)."""
    with loop.lock:
        return decision.observe_outcomes(interventions=loop.interventions, outcomes=loop.outcomes,
                                         ingestor=loop.ingestor, now=loop.clock(), after_days=after_days)


def run_refit(loop: decision.ClosedLoop, *, apply: bool = False) -> dict:
    """Refit the utility model. The ONLY path is ``decision.refit_policy`` -> the promotion gate: a candidate
    becomes active only when ``apply`` is true AND the held-out gate passes."""
    with loop.lock:
        rep = decision.refit_policy(loop.outcomes, loop.policies, apply=apply)
        return {"promoted": bool(rep.promoted), "gate_passed": bool(rep.decision.promote),
                "reasons": list(rep.decision.reasons), "candidate_policy": rep.candidate.version,
                "active_policy": loop.policies.active.version}


@router.post("/api/outcomes/observe")
async def api_outcomes_observe(request: Request) -> JSONResponse:
    require_operator(request)
    body = await _body(request)
    try:
        after = float(body.get("after_days", 7.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "after_days must be a number")
    if after < 0:
        raise HTTPException(400, "after_days must be >= 0")
    try:
        rows = run_observe(get_loop(), after_days=after)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return JSONResponse({"observed": rows, "count": len(rows)})


@router.post("/api/policy/refit")
async def api_policy_refit(request: Request) -> JSONResponse:
    require_operator(request)
    body = await _body(request)
    return JSONResponse(run_refit(get_loop(), apply=body.get("apply") is True))


# ── optional background outcome observation (BILLING_OUTCOME_INTERVAL_S; default OFF) ─────────────────────
_log = logging.getLogger("agentic_os.app")
_SCHED: dict = {"thread": None, "stop": None}


def scheduler_interval() -> float:
    try:
        v = float(os.environ.get("BILLING_OUTCOME_INTERVAL_S", "0") or 0)
    except ValueError:
        return 0.0
    return v if v > 0 else 0.0


def scheduler_tick(loop: decision.ClosedLoop) -> dict:
    """One scheduled pass: observe due outcomes. Skips (``busy``) rather than queue if another run holds the
    lock. Refit is deliberately NOT scheduled here: promotion is an explicit operator call to /api/policy/refit
    (and, when called, still goes through the gate)."""
    if not loop.lock.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        return {"observed": len(run_observe(loop))}
    except Exception as e:  # noqa: BLE001 — a Lago blip must not kill the scheduler
        _log.warning("billing outcome scheduler tick failed: %s: %s", type(e).__name__, e)
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        loop.lock.release()


def start_scheduler(loop: Optional[decision.ClosedLoop]):
    """Start the background observer when BILLING_OUTCOME_INTERVAL_S > 0 (default off). Returns the thread."""
    interval = scheduler_interval()
    if interval <= 0 or loop is None:
        return None
    stop_scheduler()
    stop = threading.Event()

    def _run():
        while not stop.wait(interval):
            scheduler_tick(loop)
    th = threading.Thread(target=_run, name="billing-outcome-scheduler", daemon=True)
    _SCHED.update(thread=th, stop=stop)
    th.start()
    return th


def stop_scheduler() -> None:
    th, stop = _SCHED.get("thread"), _SCHED.get("stop")
    if stop is not None:
        stop.set()
    if th is not None and th.is_alive():
        th.join(timeout=5)
    _SCHED.update(thread=None, stop=None)


@router.on_event("startup")
def _startup() -> None:
    """Rehydrate parked missions (build_loop does it for a durable state dir) and start the optional observer."""
    if os.environ.get("BILLING_STATE_DIR") or scheduler_interval() > 0:
        start_scheduler(get_loop()) if scheduler_interval() > 0 else get_loop()


@router.on_event("shutdown")
def _shutdown() -> None:
    stop_scheduler()


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
