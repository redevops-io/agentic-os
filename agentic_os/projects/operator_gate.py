"""An optional sign-in in front of the whole Projects app (UI, API, static files).

Projects' read routes are open by design on an evaluation deployment: the data is a sandbox. A deployment that reads a company's
real applications needs the opposite: nobody sees anything before the server has validated who they are. ``PROJECTS_OPERATOR_GATE=1``
turns that on. Every request then needs one of:

* the operator's HTTP Basic credential (``OPERATOR_BASIC_AUTH`` or ``OPERATOR_BASIC_AUTH_USER``/``_PASS``); a browser is asked for
  it once (``WWW-Authenticate``) and then sends it with every request, so the person is the same principal for reads and writes;
* a control-plane bearer (``AGENTIC_OS_CONTROL_TOKEN``, ``AGENTIC_OS_TENANT_TOKENS``, ``AGENTIC_OS_APPROVER_TOKENS``), for an API
  client. What a tenant bearer may then see is still decided per route (for the dashboards: ``AGENTIC_OS_TENANT_LIMITS``).

Fail closed: with the gate on and no credential of either kind configured, every request is answered 503; a wrong or missing
credential is 401. Only ``GET /healthz`` is exempt, and it says nothing but that the process is up.
"""
from __future__ import annotations

import json
import os

ENV_GATE = "PROJECTS_OPERATOR_GATE"
REALM = "Projects"
EXEMPT = ("/healthz",)


def gate_enabled() -> bool:
    return os.environ.get(ENV_GATE, "").strip().lower() in ("1", "true", "yes", "on")


def _configured() -> bool:
    from agentic_os.app_kit.route_authz import operator_credential  # noqa: PLC0415
    user, pw = operator_credential()
    if user and pw:
        return True
    return any(os.environ.get(k, "").strip() for k in ("AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS"))


def _valid(auth: str) -> bool:
    if not auth:
        return False
    if auth.startswith("Basic "):
        from agentic_os.app_kit.route_authz import _basic_operator  # noqa: PLC0415
        return _basic_operator(auth)
    from agentic_os.mission.control_auth import bearer_of, resolve_caller  # noqa: PLC0415
    if not bearer_of(auth):
        return False
    try:
        resolve_caller(auth)
        return True
    except Exception:  # noqa: BLE001  (HTTPException for an unknown token, ConfigError for a broken token table: both refuse)
        return False


def _cors_origin(scope) -> str:
    """The request's Origin when the operator allowed that origin to call this deployment from a browser page
    (``PROJECTS_CORS_ORIGINS``); otherwise ``""``. The same list the CORS middleware is configured with."""
    allowed = [o.strip() for o in os.environ.get("PROJECTS_CORS_ORIGINS", "").split(",") if o.strip()]
    if not allowed:
        return ""
    vals = [v for k, v in scope.get("headers", []) if k.lower() == b"origin"]
    origin = vals[0].decode("latin-1") if len(vals) == 1 else ""
    return origin if origin and ("*" in allowed or origin in allowed) else ""


def _is_preflight(scope) -> bool:
    return scope.get("method") == "OPTIONS" and any(k.lower() == b"access-control-request-method" for k, _v in scope.get("headers", []))


class OperatorGateMiddleware:
    """Pure ASGI, outermost: a refused request reaches no route, no static file and no other middleware."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        root = scope.get("root_path", "") or ""
        rel = path[len(root):] if root and path.startswith(root) else path
        if scope["type"] == "http" and scope.get("method") in ("GET", "HEAD") and rel in EXEMPT:
            return await self.app(scope, receive, send)
        # A browser asks permission (a CORS preflight) before it sends a cross-origin request with an Authorization header, and
        # by specification that question carries no credential. From an origin the operator allowed it goes to the CORS
        # middleware, which answers it and reaches no route; the real request that follows is gated like any other. From any
        # other origin it is refused here like everything else.
        origin = _cors_origin(scope) if scope["type"] == "http" else ""
        if origin and _is_preflight(scope):
            return await self.app(scope, receive, send)
        vals = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
        auth = vals[0].decode("latin-1") if len(vals) == 1 else ""          # a repeated header is refused, not guessed at
        if not _configured():
            return await self._refuse(scope, send, 503, "sign-in is required here but no credential is configured on this deployment", False, origin)
        if not _valid(auth):
            return await self._refuse(scope, send, 401, "sign in to use this deployment", True, origin)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope, send, status: int, detail: str, challenge: bool, origin: str = "") -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": {"error": "unauthenticated" if status == 401 else "unavailable", "detail": detail}}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
        if origin:
            # an allowed page may READ the refusal (so it can say "the token was not accepted" instead of "cannot connect");
            # it is not asked for the operator's password: a browser's Basic prompt belongs to this deployment's own pages
            headers += [(b"access-control-allow-origin", origin.encode("latin-1")), (b"vary", b"Origin")]
        elif challenge:
            headers.append((b"www-authenticate", f'Basic realm="{REALM}", charset="UTF-8"'.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


__all__ = ["OperatorGateMiddleware", "gate_enabled", "ENV_GATE"]
