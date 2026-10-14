"""Default-deny gate for the /missions, /capabilities and cockpit routers.

One router-level dependency (``mission_gate``) is attached to every router, so a route added later is protected
without anyone remembering to. Only depends on ``control_auth`` (+ FastAPI), so it can be copied verbatim.

* Mutating methods (anything but GET/HEAD/OPTIONS) ALWAYS need ``Authorization: Bearer <token>``. No token
  configured -> 503; missing/wrong -> 401. ``@public`` is NOT honoured for them.
* Reads are gated BY DEFAULT too. Only ``@public`` routes (static shell, capability catalogue, build info) are open.
  ``AGENTIC_OS_GATE_READS=0`` opts out (local demos on loopback only; unsafe anywhere else).
* A token that is PRESENTED but invalid is a 401 on every route (never silently treated as anonymous).
* ``@admin_only`` routes (global learner state) additionally reject tenant-scoped callers with 403.
* Tenant scoping / approver separation are applied by the handlers via ``caller_of``.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from .control_auth import Caller, any_token_configured, reads_gated, resolve_caller

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def public(fn):
    """Explicitly mark a SAFE-method route as deliberately public (open even while reads are gated)."""
    fn.__aos_public__ = True
    return fn


def admin_only(fn):
    """Mark a route as global (not tenant-scoped data): only the unscoped admin token may call it."""
    fn.__aos_admin_only__ = True
    return fn


def mission_gate(request: Request, authorization: "str | None" = Header(default=None)) -> None:
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    request.state.caller = None
    open_route = (request.method in SAFE_METHODS
                  and (getattr(endpoint, "__aos_public__", False) or not reads_gated()))
    if open_route:
        if authorization and any_token_configured():       # presented => must be valid (401), and scopes the view
            request.state.caller = resolve_caller(authorization)
    else:
        request.state.caller = resolve_caller(authorization)
    caller = request.state.caller
    if getattr(endpoint, "__aos_admin_only__", False) and caller is not None and caller.tenant is not None:
        raise HTTPException(403, "admin token required")


GATE = Depends(mission_gate)


def caller_of(request: Request) -> "Caller | None":
    return getattr(request.state, "caller", None)
