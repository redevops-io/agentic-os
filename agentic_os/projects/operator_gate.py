"""An optional sign-in in front of the whole Projects app (UI, API, static files).

Projects' read routes are open by design on an evaluation deployment: the data is a sandbox. A deployment that reads a company's
real applications needs the opposite: nobody sees anything before the server has validated who they are. ``PROJECTS_OPERATOR_GATE=1``
turns that on. Every request then needs one of:

* the operator's HTTP Basic credential (``OPERATOR_BASIC_AUTH`` or ``OPERATOR_BASIC_AUTH_USER``/``_PASS``); a browser is asked for
  it once (``WWW-Authenticate``) and then sends it with every request, so the person is the same principal for reads and writes;
* on the approval pages only, a session signed in there (``agentic_os/projects/approval_pages.py``); the sign-in page itself
  needs none, and a deep link without a session is redirected to it;
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


AGENT_PREFIX = "/api/agent-gateway/"


def _agent_may_pass(rel: str, auth: str, agent_check) -> bool:
    """An external agent's OWN credential opens the agent gateway and nothing else: the path is under the gateway (with no
    traversal in it), the header is a bearer, and the per-agent token store accepts it. The gateway then authenticates and
    authorizes it again for what it asks."""
    if agent_check is None or not rel.startswith(AGENT_PREFIX) or ".." in rel.split("/"):
        return False
    from agentic_os.mission.control_auth import bearer_of  # noqa: PLC0415
    try:
        token = bearer_of(auth)
        return bool(token) and bool(agent_check(token))
    except Exception:  # noqa: BLE001  (a store that cannot answer admits nobody)
        return False


class OperatorGateMiddleware:
    """Pure ASGI, outermost: a refused request reaches no route, no static file and no other middleware."""

    def __init__(self, app, agent_check=None, open_routes=frozenset(), agent_challenge: str = "", session_check=None) -> None:
        self.app, self.agent_check = app, agent_check
        # (relative path, Cookie header) -> a signed-in approval session opens the approval pages and nothing else
        self.session_check = session_check
        # exact (method, path) pairs an OAuth client calls before it holds any credential: discovery and its own endpoints
        self.open_routes, self.agent_challenge = frozenset(open_routes), agent_challenge

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        root = scope.get("root_path", "") or ""
        rel = path[len(root):] if root and path.startswith(root) else path
        if scope["type"] == "http" and scope.get("method") in ("GET", "HEAD") and rel in EXEMPT:
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and (scope.get("method"), rel) in self.open_routes:
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
        if scope["type"] == "http" and _agent_may_pass(rel, auth, self.agent_check):
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and self.session_check is not None:
            cookies = [v for k, v in scope.get("headers", []) if k.lower() == b"cookie"]
            if len(cookies) == 1 and self.session_check(rel, cookies[0].decode("latin-1")):
                return await self.app(scope, receive, send)
        # a person who follows a deep link without a session is sent to sign in, not shown a bare refusal
        if scope["type"] == "http" and self.session_check is not None and scope.get("method") == "GET" and not auth \
                and (rel in ("/approvals", "/oauth/authorize") or rel.startswith("/approvals/")) and ".." not in rel.split("/"):
            from urllib.parse import quote  # noqa: PLC0415
            from .approval_pages import safe_next  # noqa: PLC0415  (only a path of those pages ever reaches the header)
            then = rel + "?" + scope.get("query_string", b"").decode("latin-1") if rel == "/oauth/authorize" else rel
            target = (root.rstrip("/") + "/approvals/signin?next=" + quote(safe_next(then), safe="/")).encode("ascii", "ignore")
            await send({"type": "http.response.start", "status": 303, "headers": [(b"location", target), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if not _configured():
            return await self._refuse(scope, send, 503, "sign-in is required here but no credential is configured on this deployment", False, origin)
        if not _valid(auth):
            agent = self.agent_challenge if rel.startswith(AGENT_PREFIX) else ""
            return await self._refuse(scope, send, 401, "sign in to use this deployment", True, origin, agent)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope, send, status: int, detail: str, challenge: bool, origin: str = "", agent_challenge: str = "") -> None:
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
            headers.append((b"www-authenticate", (agent_challenge or f'Basic realm="{REALM}", charset="UTF-8"').encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


__all__ = ["OperatorGateMiddleware", "gate_enabled", "ENV_GATE"]
