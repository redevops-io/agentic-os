"""Plan approval bound to the exact plan: ``plan_sha256`` AND the exact step set and targets.

The runtime's own approval (``MissionRuntime.approve``) records that a human cleared a parked node and binds the inputs the
human was shown. This module adds the statement the owner asked for: the approver signs *this plan hash* and *this list of
(step, capability, op id, target digest)*. Every side-effecting step re-verifies it at execution time against the plan the
ledger holds NOW, so a changed plan, an added, removed or retargeted step, another mission's approval, or an expired approval
fails closed before any request is sent. HMAC-SHA256 with a key domain-separated from the platform's approval and
execution-authorization keys."""
from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
import time

from . import opid
from .context import plan_sha256, step_binding

KIND = "aosdd.plan_approval/1"
DEFAULT_TTL_S = 900


def _key(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"redevops/aosdd/plan-approval/v1", hashlib.sha256).digest()


def sign(secret: str, claims: dict) -> str:
    return hmac.new(_key(secret), opid.canonical(claims).encode(), hashlib.sha256).hexdigest()


def mint(secret: str, mission_id: str, plan: dict, approver: str, *, ttl_s: int = DEFAULT_TTL_S, now: "float | None" = None) -> dict:
    if not str(approver or "").strip():
        raise ValueError("the approver must be named")
    t = time.time() if now is None else now
    claims = {"kind": KIND, "mission_id": mission_id, "plan_sha256": plan_sha256(plan), "steps": step_binding(plan),
              "approver": str(approver).strip(), "iat": t, "exp": t + ttl_s, "nonce": _secrets.token_hex(12)}
    return {"claims": claims, "sig": sign(secret, claims)}


def verify(secret: str, approval: "dict | None", mission_id: str, plan: dict, *, now: "float | None" = None) -> "tuple[bool, str]":
    """(ok, reason). Recomputes the plan hash and the step binding from ``plan`` (the plan the ledger holds now)."""
    try:
        if not isinstance(approval, dict) or not isinstance(approval.get("claims"), dict):
            return False, "no plan approval is recorded for this mission"
        c = approval["claims"]
        if not hmac.compare_digest(str(approval.get("sig", "")), sign(secret, c)):
            return False, "the plan approval signature does not verify"
        if c.get("kind") != KIND:
            return False, "not a plan approval"
        if c.get("mission_id") != mission_id:
            return False, "the approval belongs to a different mission (replay)"
        t = time.time() if now is None else now
        if not (float(c["exp"]) > t):
            return False, "the plan approval has expired"
        if plan.get("mission_id") != mission_id or plan_sha256(plan) != plan.get("plan_sha256"):
            return False, "the plan does not hash to its own plan_sha256 (it was changed)"
        if c.get("plan_sha256") != plan.get("plan_sha256"):
            return False, "the approved plan_sha256 is not the plan that is about to run"
        if c.get("steps") != step_binding(plan):
            return False, "the approved step set or targets differ from the plan that is about to run"
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - any malformed input fails closed
        return False, f"approval unreadable: {type(e).__name__}"
