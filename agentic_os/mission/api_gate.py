"""Default-deny gate for the /missions, /capabilities and cockpit routers.

One router-level dependency (``mission_gate``) is attached to every router, so a route added later is protected
without anyone remembering to. Rules:

* Any method other than GET/HEAD/OPTIONS (POST/PUT/PATCH/DELETE ...) ALWAYS needs ``Authorization: Bearer <token>``
  (admin ``AGENTIC_OS_CONTROL_TOKEN`` or a tenant token from ``AGENTIC_OS_TENANT_TOKENS``). No token configured ->
  503; missing/wrong -> 401. ``@public`` is NOT honoured for mutating methods.
* Safe methods are open by default (backward compatible) EXCEPT that setting ``AGENTIC_OS_GATE_READS=1`` gates
  every safe route not decorated ``@public``. Routes that expose mission data (list/get/plans/events/explain/
  inbox/learning) are deliberately NOT ``@public``.
* A tenant-scoped caller is bound to its tenant: see ``api.py`` (cross-tenant -> 404, no existence leak).
"""
from __future__ import annotations

from fastapi import Depends, Header, Request

from .control_auth import Caller, reads_gated, resolve_caller

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def public(fn):
    """Explicitly mark a SAFE-method route as deliberately public (readable even with AGENTIC_OS_GATE_READS=1)."""
    fn.__aos_public__ = True
    return fn


def mission_gate(request: Request, authorization: "str | None" = Header(default=None)) -> None:
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    if request.method in SAFE_METHODS:
        if getattr(endpoint, "__aos_public__", False) or not reads_gated():
            request.state.caller = None
            if authorization:                      # open read: a VALID presented token still scopes the view
                try:
                    request.state.caller = resolve_caller(authorization)
                except Exception:  # noqa: BLE001 - open route: bad/absent config just means unscoped
                    pass
            return
    request.state.caller = resolve_caller(authorization)


GATE = Depends(mission_gate)


def caller_of(request: Request) -> "Caller | None":
    return getattr(request.state, "caller", None)
