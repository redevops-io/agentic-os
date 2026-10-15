"""The authenticated ``/invoke`` + ``/capabilities`` surface.

``Operator.router()`` is the open "try" surface: it has no caller authentication of its own. This module mounts that
SAME router (so the signed approval-token handling -- strip every client ``_approval*`` key, re-inject the marker only
from a token that verifies for operator / capability / inputs / mission / node, single use via the replay guard -- is
the kernel's, unmodified) behind the kernel's ``mission_gate``:

  * every request needs ``Authorization: Bearer <token>`` (default-deny; no token configured -> 503;
    missing / wrong -> 401);
  * a tenant-scoped token pins the caller to its tenant: a conflicting ``tenant`` input is a 403, and the
    AUTHENTICATED tenant/actor is injected into the inputs by ``GatedOperator.invoke`` AFTER the approval token was
    verified -- a client can never choose them (client ``_auth*`` keys are stripped).
"""
from __future__ import annotations

import contextvars

from fastapi import APIRouter, Depends, HTTPException, Request

from agentic_os.mission.api_gate import GATE, caller_of
from agentic_os.mission.operator_sdk import Operator

_CALLER: contextvars.ContextVar = contextvars.ContextVar("docedit_caller", default=None)


def _is_auth_key(k) -> bool:
    return isinstance(k, str) and k.lstrip("_").lower().startswith("auth")


async def _bind_caller(request: Request):
    token = _CALLER.set(caller_of(request))
    try:
        yield
    finally:
        _CALLER.reset(token)


class GatedOperator(Operator):
    """An Operator whose ``invoke`` binds the authenticated caller. In-process (trusted runtime) calls carry no HTTP
    caller and pass ``tenant`` explicitly; client-supplied ``_auth*`` keys are removed in every case."""

    def invoke(self, capability: str, inputs: dict, idempotency_key: str = "", *, secrets: "dict | None" = None) -> dict:
        clean = {k: v for k, v in (inputs or {}).items() if not _is_auth_key(k)}
        caller = _CALLER.get()
        if caller is not None:
            if caller.tenant is not None:
                if clean.get("tenant") not in (None, caller.tenant):
                    raise HTTPException(403, "tenant does not match the authenticated tenant")
                clean["_auth_tenant"] = caller.tenant
            clean["_auth_actor"] = f"{caller.role}:{caller.tenant or 'admin'}"
        if idempotency_key:
            clean["_auth_idem"] = idempotency_key
        return super().invoke(capability, clean, idempotency_key, secrets=secrets)


def secure_router(operator: Operator) -> APIRouter:
    outer = APIRouter(dependencies=[GATE, Depends(_bind_caller)])
    outer.include_router(operator.router())
    return outer
