"""Direct-action authorization: an authenticated user acts WITHOUT building a mission, but never without authority.

The caller names an operator, a capability and the exact inputs. This evaluates them against the owner's envelope
(the same guard, ledger and rules the mission runtime uses) and, only if allowed, returns a short-lived execution
authorization bound to those inputs (execution_auth.py). The reservation stays RESERVED until the OPERATOR consumes the
authorization and performs the dispatch, so an unused authorization costs nothing once it expires (it can be swept).

A capability that declares a mandatory approval is never authorized here: that stays a human decision. The result is
auditable end to end: tenant, subject, envelope digest, lease id, expiry.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from .approval import strip_approval_keys
from .authority import Action, AuthorityGuard, Verdict
from .execution_auth import DEFAULT_TTL_S, mint_execution_authorization, strip_exec_auth_keys


class ActionAuthorizer:
    def __init__(self, guard: AuthorityGuard, registry, *, key_for: "Callable[[str, str], str] | None" = None,
                 secret: "bytes | str | None" = None, clock=time.time) -> None:
        self.guard = guard
        self.registry = registry
        self.key_for = key_for or (lambda tenant, subject: f"{tenant}:direct")
        self.secret = secret
        self.clock = clock

    def authorize(self, *, tenant: str, subject: str, operator: str, capability: str, inputs: dict,
                  idempotency_key: str = "", ttl_s: int = DEFAULT_TTL_S) -> dict:
        def refuse(rule: str, reason: str, verdict: str = "deny", amendment: "dict | None" = None) -> dict:
            return {"authorized": False, "verdict": verdict, "rule": rule, "reason": reason, "amendment": amendment}

        if not tenant:
            return refuse("no_tenant", "a tenant is required")
        spec = self.registry.get(capability)
        if spec is None or (spec.operator and spec.operator != operator):
            return refuse("unknown_capability", "no such capability on that operator")     # same answer for both: no probing
        if getattr(spec, "approval_required", False):
            return refuse("mandatory_approval", "this capability always needs a human approval; it cannot be authorized "
                                                "by a standing grant or a direct action", "escalate")
        clean = strip_exec_auth_keys(strip_approval_keys(dict(inputs or {})))
        side = bool(spec.side_effecting)
        irreversible = side and not (getattr(spec, "undo", None) or getattr(spec, "compensation", None))
        action = Action(capability=capability, arguments={k: v for k, v in clean.items() if not str(k).startswith("_")},
                        risk_tier=2 if irreversible else (1 if side else 0), side_effecting=side,
                        cost_usd=float(getattr(getattr(spec, "cost", None), "usd", 0.0) or 0.0))
        lease = f"direct:{uuid.uuid4().hex}"
        res = self.guard.authorize(self.key_for(tenant, subject), tenant, action, lease_id=lease)
        if not res.allowed:
            return refuse(res.rule, res.reason, res.verdict.value, res.amendment)
        key = idempotency_key or lease                      # the operator requires this exact key on the call
        token = mint_execution_authorization(
            self.secret, operator=operator, capability=capability, tenant=tenant, mission_id=lease, node_id="direct",
            lease_id=lease, attempt=1, envelope_digest=res.envelope_digest, inputs=clean, idempotency_key=key,
            ttl_s=ttl_s, now=self.clock(), dispatch_by="operator")
        if not token:
            self.guard.release(lease)                       # nothing was issued: give the reservation back
            return refuse("no_signing_key", "the platform signing secret is not configured")
        return {"authorized": True, "authorization": token, "idempotency_key": key, "lease_id": lease,
                "expires_in_s": ttl_s, "envelope_digest": res.envelope_digest}


def build_action_router(runtime) -> Any:
    """`POST /actions/authorize` (tenant or admin credential). Mounted by the mission service when the runtime has an
    authority guard; otherwise there is nothing to evaluate against and the route is not exposed."""
    from fastapi import APIRouter, HTTPException, Header  # noqa: PLC0415
    from pydantic import BaseModel  # noqa: PLC0415
    from .control_auth import resolve_caller  # noqa: PLC0415

    router = APIRouter(tags=["actions"])
    authorizer = ActionAuthorizer(runtime.authority, runtime.registry)

    class Body(BaseModel):
        operator: str
        capability: str
        inputs: dict = {}
        idempotency_key: str = ""
        tenant: str | None = None            # admin credentials only

    @router.post("/actions/authorize")
    def authorize(body: Body, authorization: str | None = Header(default=None)):
        caller = resolve_caller(authorization)
        if caller.role == "approver":
            raise HTTPException(403, "approver credentials cannot request action authorizations")
        tenant = caller.tenant if caller.tenant is not None else (body.tenant or "")
        if caller.tenant is not None and body.tenant not in (None, "", caller.tenant):
            raise HTTPException(403, "tenant does not match the authenticated credential")
        out = authorizer.authorize(tenant=tenant, subject=caller.role, operator=body.operator,
                                   capability=body.capability, inputs=body.inputs, idempotency_key=body.idempotency_key)
        if not out["authorized"]:
            raise HTTPException(403 if out["verdict"] == "deny" else 409, out)
        return out

    return router
