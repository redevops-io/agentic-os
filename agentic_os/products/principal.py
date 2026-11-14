"""Who is acting in Projects, for attribution of a human decision.

A decision a person makes in Projects counts as the product's approval only when Projects can name that person with a
credential the SERVER validated; the request body never says who approved. The principal comes from, in order:

1. the desktop capability gate: it writes the session it has just validated into the ASGI scope under
   ``agentic_os.local_session`` (only the gate can write it; a header, query or body cannot) -> ``local-owner``;
2. a control credential in ``Authorization: Bearer`` (hosted deployments): ``admin``, ``approver:<tenant>`` or
   ``requester:<tenant>`` exactly as ``agentic_os.mission.control_auth`` resolves it (a requester is refused while
   approver credentials exist: separation of duties, the same rule as the Content approve routes);
3. the ``principal.single_owner`` the OPERATOR declared in the products registry, for a deployment that only one person
   can reach (a loopback Compose, the desktop). Never set it on a Projects that others can reach.

No principal => the decision is refused (401) and nothing is sent to the product. The principal is then mapped to a
LINKED product user by the product itself (``actor_links``); an unlinked principal is refused there (403).
"""
from __future__ import annotations

import contextvars
from typing import Any, Dict, Optional

SESSION_SCOPE_KEY = "agentic_os.local_session"
LOCAL_OWNER = "local-owner"

_SCOPE: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar("products_scope", default=None)


class PrincipalMiddleware:
    """Pure-ASGI: remembers the request scope for the duration of the call so provider methods (which the Projects
    routes call without a request object) can ask who is acting."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = _SCOPE.set(scope)
        try:
            await self.app(scope, receive, send)
        finally:
            _SCOPE.reset(token)


def _header(scope: Dict[str, Any], name: bytes) -> str:
    vals = [v for k, v in scope.get("headers", []) if k.lower() == name]
    return vals[0].decode("latin-1") if len(vals) == 1 else ""      # a repeated header is a smuggling signal: ignore it


def current_principal(single_owner: str = "") -> Optional[str]:
    scope = _SCOPE.get()
    if scope is not None:
        if scope.get(SESSION_SCOPE_KEY) is not None:
            return LOCAL_OWNER
        auth = _header(scope, b"authorization")
        if auth:
            from agentic_os.mission.control_auth import ConfigError, approver_tokens, bearer_of, resolve_caller  # noqa: PLC0415
            from fastapi import HTTPException  # noqa: PLC0415
            if bearer_of(auth):
                try:
                    caller = resolve_caller(auth)
                except HTTPException:
                    return None
                except ConfigError:
                    return None
                try:
                    has_approvers = bool(approver_tokens())
                except ConfigError:
                    has_approvers = True
                if caller.role == "requester" and has_approvers:
                    return None
                return "admin" if caller.role == "admin" else f"{caller.role}:{caller.tenant}"
    return single_owner or None


def current_actor_label(single_owner: str = "") -> str:
    """The principal as the acting-user claim sent on reads/staging (informational; never a credential)."""
    p = current_principal(single_owner)
    return f"projects:{p}" if p else "projects:anonymous"


__all__ = ["PrincipalMiddleware", "current_principal", "current_actor_label", "SESSION_SCOPE_KEY", "LOCAL_OWNER"]
