"""One enforcement layer for an app's own REST write routes.

Every state-changing route declares a `RoutePolicy`; one ASGI middleware applies it. There are three authorization
CONTEXTS and they obtain authority differently, but all pass the same policy enforcement point:

* human   - an authenticated person: the app's operator credential (HTTP Basic), or a bearer tenant token. A tenant token
            identifies a tenant; it does NOT confer every permission: it needs the route's permission among the refs that
            tenant was granted (`AGENTIC_OS_TENANT_LIMITS`).
* agent   - a runtime/direct-action issued execution authorization (`X-Execution-Authorization`), bound to this app, this
            route's capability, the tenant, an idempotency key and the exact request (body + path parameters), single-use
            and replay-protected. It can never stand in for a human decision, nor for an action the route marks human-only.
* service - an internal workload identity (`X-Service-Token`) holding explicit capabilities
            (`AGENTIC_OS_SERVICE_TOKENS`), for platform components that call the app.

Access classes (per route):
  read_only           a POST used for a query; no state change
  public_intake       unauthenticated BY DESIGN (e.g. a data-subject request form); needs a written rationale; records only
  stages_only         the handler stages a preview unless handed an approval marker; the marker comes ONLY from an
                      authenticated context (never from the body); an AGENT gets one only for an action the policy lists in
                      `agent_actions` (default: none), so a multiplexed route never lets an agent approve what it was not given
  authority_required  a real side effect: human with the permission, an agent authorization, or a service capability
  human_only          a human decision (approve, execute): agents and services are refused
  operator_boundary   the operator's `/invoke`, which enforces the execution-authorization boundary itself

In the production profile a request that fails its policy is refused BEFORE the handler runs, and a write route with no
policy is refused outright. In other profiles the context is still computed (so a standing grant works the same through
REST) but nothing is blocked. A CI test fails when a write route is added without a policy.
"""
from __future__ import annotations

import base64
import contextvars
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from agentic_os.mission.approval import strip_approval_keys
from agentic_os.mission.execution_auth import strip_exec_auth_keys, verify_execution_authorization
from agentic_os.mission.profile import is_production

log = logging.getLogger("agentic_os.route_authz")

ACCESS = ("read_only", "public_intake", "stages_only", "authority_required", "human_only", "operator_boundary")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
HEADER_EXEC = "x-execution-authorization"
HEADER_SERVICE = "x-service-token"
MAX_BODY = 1_000_000

_marker: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar("route_authz_marker", default=None)


@dataclass(frozen=True)
class RoutePolicy:
    capability: str
    access: str
    permission: str = ""                      # default "<app>:write" (set at install)
    agent_actions: tuple = ()                 # body.action values an AGENT may approve on this route (allowlist; default none)
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.access not in ACCESS:
            raise ValueError(f"access must be one of {ACCESS}, got {self.access!r}")
        if self.access in ("public_intake", "read_only", "stages_only") and not self.rationale.strip():
            raise ValueError(f"{self.capability}: access '{self.access}' needs a written rationale")
        if self.access in ("authority_required", "human_only", "stages_only") and not self.capability:
            raise ValueError("a capability name is required")


@dataclass
class AuthzContext:
    kind: str = "anonymous"                   # anonymous | human | agent | service
    subject: str = ""
    tenant: "str | None" = None
    permissions: tuple = ()
    capabilities: tuple = ()
    claims: "dict | None" = None
    credential_rejected: bool = False         # a credential was presented but did not verify
    approval_marker: "dict | None" = None


def current_marker() -> "dict | None":
    """The approval marker the CURRENT request earned from an authenticated context, else None. Apps' existing marker helpers
    OR this with their own operator-credential check; a request body can never supply it."""
    return _marker.get()


def route_inputs(body: Any, path_params: "dict | None") -> dict:
    """The canonical effective inputs of a REST call: JSON-object body (approval/authorization keys stripped) plus path
    parameters. Issuer and verifier must agree; this is the one definition."""
    base = dict(body) if isinstance(body, dict) else {}
    out = strip_exec_auth_keys(strip_approval_keys(base))
    if path_params:
        out["_path_params"] = {str(k): str(v) for k, v in path_params.items()}
    return out


def operator_credential() -> tuple:
    user = os.environ.get("OPERATOR_BASIC_AUTH_USER", "")
    pw = os.environ.get("OPERATOR_BASIC_AUTH_PASS", "")
    if not (user and pw):
        combo = os.environ.get("OPERATOR_BASIC_AUTH") or os.environ.get("OUTREACH_BASIC_AUTH") or ""
        user, _, pw = combo.partition(":")
    return user, pw


def _basic_operator(auth_header: str) -> bool:
    user, pw = operator_credential()
    if not (user and pw) or not auth_header.startswith("Basic "):
        return False
    try:
        gu, _, gp = base64.b64decode(auth_header[6:]).decode("utf-8", "replace").partition(":")
    except Exception:  # noqa: BLE001
        return False
    return bool(hmac.compare_digest(gu.encode(), user.encode()) & hmac.compare_digest(gp.encode(), pw.encode()))


def service_identities() -> dict:
    """`AGENTIC_OS_SERVICE_TOKENS`: {"console": {"token": "...", "capabilities": ["billing.*"]}}. Malformed means none."""
    raw = os.environ.get("AGENTIC_OS_SERVICE_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return {str(k): {"token": str(v["token"]), "capabilities": tuple(v.get("capabilities", ()))}
                for k, v in d.items() if isinstance(v, dict) and v.get("token")}
    except Exception:  # noqa: BLE001
        return {}


def _granted(perm: str, refs) -> bool:
    return any(fnmatchcase(perm, r) for r in refs)


def authenticate(headers: "dict[str, str]", *, app_name: str, policy: RoutePolicy, body: Any, path_params: dict,
                 replay_guard) -> AuthzContext:
    """Classify the caller. An execution authorization wins over every other credential on the request: a request that
    carries one is an AGENT request and gets no human privileges from any other header."""
    exec_tok = headers.get(HEADER_EXEC)
    if exec_tok:
        claims = verify_execution_authorization(
            None, exec_tok, operator=app_name, capability=policy.capability, inputs=route_inputs(body, path_params),
            idempotency_key=headers.get("idempotency-key", ""), replay_guard=replay_guard,
            tenant=os.environ.get("AGENTIC_OS_TENANT", "").strip() or None)      # a deployment bound to a tenant refuses others
        if claims is None:
            return AuthzContext(credential_rejected=True)
        return AuthzContext(kind="agent", subject=claims["lease_id"], tenant=claims["tenant"], claims=claims)
    svc = headers.get(HEADER_SERVICE)
    if svc:
        for name, ident in service_identities().items():
            if hmac.compare_digest(svc.encode(), ident["token"].encode()):
                return AuthzContext(kind="service", subject=name, capabilities=ident["capabilities"])
        return AuthzContext(credential_rejected=True)
    auth = headers.get("authorization", "")
    if _basic_operator(auth):
        return AuthzContext(kind="human", subject="operator", permissions=("*",),
                            approval_marker={"approved": True, "by": "operator-basic"})
    if auth.startswith("Bearer "):
        from agentic_os.mission.control_auth import resolve_caller, tenant_limits
        try:
            caller = resolve_caller(auth)
        except Exception:  # noqa: BLE001 - HTTPException: not a configured token
            return AuthzContext(credential_rejected=True)
        if caller.role == "admin":
            return AuthzContext(kind="human", subject="admin", permissions=("*",),
                                approval_marker={"approved": True, "by": "admin-token"})
        refs = tuple(tenant_limits(caller.tenant)[0]) if caller.tenant else ()
        return AuthzContext(kind="human", subject=f"{caller.role}:{caller.tenant}", tenant=caller.tenant,
                            permissions=refs,
                            approval_marker={"approved": True, "by": f"tenant:{caller.tenant}"})
    return AuthzContext()


def decide(policy: RoutePolicy, ctx: AuthzContext, body: Any, *, app_name: str) -> "tuple[bool, int, str, dict | None]":
    """(allowed, http status if refused, reason, approval marker for the handler). Pure."""
    action = str((body or {}).get("action", "")) if isinstance(body, dict) else ""
    perm = policy.permission or f"{app_name}:write"
    agent_ok = action in policy.agent_actions
    marker = None
    if ctx.kind == "human" and _granted(perm, ctx.permissions):
        marker = ctx.approval_marker                       # a person with the permission may approve
    elif ctx.kind == "agent" and agent_ok:
        marker = {"approved": True, "by": "execution-authorization", "lease": ctx.subject}
    access = policy.access
    if access in ("read_only", "public_intake", "operator_boundary"):
        return True, 200, "", None
    if access == "stages_only":
        return True, 200, "", marker
    if ctx.credential_rejected:
        return False, 403, "the presented credential did not verify", None
    if ctx.kind == "anonymous":
        return False, 401, "authentication and authority are required for this action", None
    if access == "human_only":
        if ctx.kind != "human" or not _granted(perm, ctx.permissions):
            return False, 403, "this is a human decision and needs a person holding the permission", None
        return True, 200, "", marker
    # authority_required
    if ctx.kind == "human":
        if not _granted(perm, ctx.permissions):
            return False, 403, f"this credential does not hold the '{perm}' permission", None
        return True, 200, "", marker
    if ctx.kind == "agent":
        return True, 200, "", marker
    if ctx.kind == "service":
        if not any(fnmatchcase(policy.capability, c) for c in ctx.capabilities):
            return False, 403, "this service identity lacks the capability", None
        return True, 200, "", None
    return False, 403, "refused", None


class RouteAuthzMiddleware:
    """Pure ASGI (so the request-scoped marker reaches sync handlers running in the threadpool)."""

    def __init__(self, app, *, owner, app_name: str, policies: dict, replay_guard=None) -> None:
        self.app, self.owner, self.app_name, self.policies = app, owner, app_name, policies
        self.replay_guard = replay_guard
        self._warned: set = set()

    def _templates(self):
        """Policy keys compiled with Starlette's own path compiler, most specific first. Matching against the declared
        templates (not the framework's route tree) keeps this independent of how FastAPI nests included routers."""
        if not hasattr(self, "_compiled"):
            from starlette.routing import compile_path
            rows = []
            for key in self.policies:
                method, _, template = key.partition(" ")
                regex, _fmt, convertors = compile_path(template)
                rows.append((template.count("{"), len(template), method, template, regex, convertors, key))
            self._compiled = sorted(rows, key=lambda r: (r[0], -r[1]))
        return self._compiled

    @staticmethod
    def _route_path(scope) -> str:
        """The path the app's own routes are declared against. When the app is mounted under a base path (`/projects`), the server keeps the
        full path and records the prefix in `root_path`; matching must be against what is left, or every request would look like a route
        nobody classified (and be refused in production)."""
        path, root = scope["path"], scope.get("root_path", "") or ""
        if root and path.startswith(root.rstrip("/") + "/"):
            return path[len(root.rstrip("/")):]
        return path

    def _match(self, scope):
        method = "WS" if scope["type"] == "websocket" else scope["method"]
        path = self._route_path(scope)
        for _n, _l, m, template, regex, convertors, key in self._templates():
            if m != method:
                continue
            hit = regex.match(path)
            if hit:
                return key, {k: convertors[k].convert(v) for k, v in hit.groupdict().items()}
        return None, {}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or (scope["type"] == "http" and scope["method"] not in WRITE_METHODS):
            return await self.app(scope, receive, send)
        key, path_params = self._match(scope)
        policy = self.policies.get(key) if key else None
        if policy is None:
            # A write request that matches no declared policy: either a real route nobody classified, or a path that
            # does not exist. Production refuses both (fail closed); the CI registry test catches the first.
            label = f"{'WS' if scope['type'] == 'websocket' else scope['method']} {scope['path']}"
            if is_production():
                return await self._refuse(scope, send, 403, f"{label} has no authorization policy")
            if label not in self._warned:
                self._warned.add(label)
                log.warning("write request %s to %s matches no RoutePolicy (refused in production)", label, self.app_name)
            return await self.app(scope, receive, send)
        body_bytes, receive = await self._buffer(scope, receive) if scope["type"] == "http" else (b"", receive)
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except Exception:  # noqa: BLE001
            body = {}
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        ctx = authenticate(headers, app_name=self.app_name, policy=policy, body=body, path_params=path_params,
                           replay_guard=self.replay_guard)
        allowed, status, reason, marker = decide(policy, ctx, body, app_name=self.app_name)
        scope.setdefault("state", {})["authz"] = ctx
        if not allowed and is_production():
            return await self._refuse(scope, send, status, reason)
        token = _marker.set(marker)
        try:
            return await self.app(scope, receive, send)
        finally:
            _marker.reset(token)

    @staticmethod
    async def _buffer(scope, receive):
        chunks, more = [], True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
            if sum(map(len, chunks)) > MAX_BODY:
                break
        data = b"".join(chunks)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()
        return data, replay

    @staticmethod
    async def _refuse(scope, send, status, reason):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        payload = json.dumps({"error": "unauthorized" if status == 401 else "forbidden", "reason": reason}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        if status == 401:
            headers.append((b"www-authenticate", b'Basic realm="operator"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


def _walk(routes, out):
    """Flatten a router tree. Handles both flattened routes and FastAPI's lazily-included routers
    (`effective_candidates()` yields each included route with its full, prefixed path)."""
    for r in routes:
        if hasattr(r, "effective_candidates"):
            _walk(r.effective_candidates(), out)
            continue
        route = getattr(r, "original_route", r)
        path = getattr(r, "path", "") or getattr(getattr(r, "starlette_route", None), "path", "") or getattr(route, "path", "")
        if not path:
            continue
        if route.__class__.__name__ in ("WebSocketRoute", "APIWebSocketRoute"):
            out.add(f"WS {path}")
        for m in (getattr(route, "methods", None) or set()) & WRITE_METHODS:
            out.add(f"{m} {path}")


def write_routes(app) -> "list[str]":
    """Every state-changing HTTP route and every WebSocket route the app really serves, as 'METHOD /path'."""
    out: set = set()
    _walk(app.router.routes, out)
    return sorted(out)


def unclassified(app, policies: dict) -> "list[str]":
    return [r for r in write_routes(app) if r not in policies]


def install(app, app_name: str, policies: dict, *, replay_guard=None) -> None:
    """Apply `policies` ({'POST /path': RoutePolicy}) to `app`. Safe to call once per app, after the app object exists."""
    from agentic_os.mission.approval import default_replay_guard
    norm = {k: (v if v.permission else RoutePolicy(v.capability, v.access, f"{app_name}:write", v.agent_actions, v.rationale))
            for k, v in policies.items()}
    app.add_middleware(RouteAuthzMiddleware, owner=app, app_name=app_name, policies=norm,
                       replay_guard=replay_guard or default_replay_guard())
    app.state.route_policies = norm


class RouteCatalog:
    """Lets the direct-action authorizer see REST route capabilities next to the mission registry's: an agent obtains an
    authorization for 'billing.agent_run' the same way it does for an operator capability."""

    def __init__(self, base=None, apps: "dict | None" = None) -> None:
        self.base, self.apps = base, dict(apps or {})

    def add_app(self, app_name: str, policies: dict) -> None:
        self.apps[app_name] = policies

    def get(self, capability: str):
        spec = self.base.get(capability) if self.base is not None else None
        if spec is not None:
            return spec
        from types import SimpleNamespace
        for app_name, pols in self.apps.items():
            for pol in pols.values():
                if pol.capability == capability and pol.access in ("authority_required", "stages_only"):
                    return SimpleNamespace(operator=app_name, side_effecting=True, approval_required=False, undo=None,
                                           compensation=None, cost=None, route_access=pol.access,
                                           agent_actions=pol.agent_actions)
        return None
