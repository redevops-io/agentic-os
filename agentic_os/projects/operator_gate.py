"""An optional sign-in in front of the whole Projects app (UI, API, static files).

Projects' read routes are open by design on an evaluation deployment: the data is a sandbox. A deployment that reads a company's
real applications needs the opposite: nobody sees anything before the server has validated who they are. ``PROJECTS_OPERATOR_GATE=1``
turns that on. Every request then needs one of:

* the operator's HTTP Basic credential (``OPERATOR_BASIC_AUTH`` or ``OPERATOR_BASIC_AUTH_USER``/``_PASS``); a browser is asked for
  it once (``WWW-Authenticate``) and then sends it with every request, so the person is the same principal for reads and writes;
* a control-plane bearer (the admin, tenant or approver token tables of ``agentic_os.mission.control_auth``), for an API
  client. What a tenant bearer may then see is still decided per route (for the dashboards: its tenant limits).

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
    from agentic_os.mission import control_auth as _ca  # noqa: PLC0415  (the variable names live with the code that reads them)
    return any(os.environ.get(k, "").strip() for k in (_ca.ENV_TOKEN, _ca.ENV_TENANT_TOKENS, _ca.ENV_APPROVER_TOKENS))


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
        vals = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
        auth = vals[0].decode("latin-1") if len(vals) == 1 else ""          # a repeated header is refused, not guessed at
        if not _configured():
            return await self._refuse(scope, send, 503, "sign-in is required here but no credential is configured on this deployment", False)
        if not _valid(auth):
            return await self._refuse(scope, send, 401, "sign in to use this deployment", True)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope, send, status: int, detail: str, challenge: bool) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": {"error": "unauthenticated" if status == 401 else "unavailable", "detail": detail}}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
        if challenge:
            headers.append((b"www-authenticate", f'Basic realm="{REALM}", charset="UTF-8"'.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


__all__ = ["OperatorGateMiddleware", "gate_enabled", "ENV_GATE"]
