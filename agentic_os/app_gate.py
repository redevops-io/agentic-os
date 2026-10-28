"""Start-up wiring of an app's permissions gate: what an app does at import to join a permissions plane.

Two different things, never confused (the feature boundary):

* **BASIC permissions** are an AGPL feature of this repository and need NOTHING from ``context_runtime_enterprise``: grants over tools and
  resources, the trusted ``tool_actions`` table, basic row/column restrictions (within their documented enforcement scope) and a LOCAL
  single-user identity, all through the kernel's own plane (``PermissionsPlane`` / ``GrantStore`` / ``make_authorizer``). Switch:
  ``AGENTIC_OS_PERMISSIONS=1`` (set by the ``agentic-os permissions init`` next-step line).
* **ENTERPRISE identity and permissioning** (SSO / tenant identity, ``CR_IDENTITY_*`` / ``CR_IDENTITY_MODE``, the ``CR_PERMISSIONS`` enterprise
  bootstrap, permission-aware retrieval and cache lifecycle) are Enterprise capabilities. Requested while the package is absent they FAIL
  CLOSED with a clear missing-capability error that names the capability and the fix.

Behaviour (``install_gate(app)``; each row is tested with the enterprise package absent and again with a stub present):

| requested                                      | enterprise package absent                              | enterprise package present                     |
|------------------------------------------------|--------------------------------------------------------|------------------------------------------------|
| nothing                                        | basic default: governance, approvals, budgets,         | the SAME basic default (the package is         |
|                                                | audit work; app tools are not authorised (as before)   | never imported: the kernel never imports it    |
|                                                |                                                        | unless an enterprise capability is requested)  |
| ``AGENTIC_OS_PERMISSIONS=1`` (basic)           | kernel authorizer over the app's tools; the principal  | the same (basic never needs the package; the   |
|                                                | is the LOCAL profile, never a request header; no admin | enterprise table is consulted only to merge)   |
|                                                | bypass; no grants yet = the app STARTS RESTRICTED      |                                                |
| ``CR_PERMISSIONS`` / ``CR_IDENTITY_*`` / the   | ``EnterpriseCapabilityMissing``: the app refuses to    | enterprise bootstrap; any failure refuses to   |
| permission-aware retrieval / cache switches    | start, naming the capability and the fix               | start                                          |

Basic mode and "no grants provisioned": a missing key is NOT a start-up failure (a missing local profile is not one either: the principal is then the default local user): the app starts RESTRICTED, every protected
operation is denied with a message that points at ``agentic-os permissions init``, and nothing protected ever runs without its grant. Any other
reason the gate cannot be installed (an unresolvable or conflicting tool table, a store that cannot be opened with the key that IS provisioned) is a
``GateStartupError``: the app does not start. Nothing here swallows an error with a broad ``except``.

Where authorisation is enforced: the harness ``ToolRegistry`` / the ``Assistant`` consoles through the process authorizer (``default_authorizer``);
the Operator SDK where a capability EXECUTES (``operator_denial``); and each route that executes a tool without a console through
``require_tool``. Authorisation and approval stay separate checks.

``tool_actions`` come from ONE resolver (``agentic_os.tool_actions.resolve_tool_actions``): the kernel's table; the enterprise table is merged
only when the enterprise package is present, never required (checked for conflicts, never copied).
"""
from __future__ import annotations

import importlib
import inspect
import logging
import os
import contextvars
import sys
import threading
import weakref
from dataclasses import dataclass
from types import SimpleNamespace

from . import trusted_import

_log = logging.getLogger("agentic_os.app_gate")

_OFF = frozenset({"", "0", "false", "no", "off"})
_ON = frozenset({"1", "true", "yes", "on"})
IDENTITY_PREFIX = "CR_IDENTITY_"
BASIC_SWITCH = "AGENTIC_OS_PERMISSIONS"
RETRIEVAL_SWITCH = "AGENTIC_OS_PERMISSION_AWARE_RETRIEVAL"
CACHE_SWITCH = "AGENTIC_OS_PERMISSION_AWARE_CACHE"
INIT_HINT = ("basic permissions are on but no grant allows this: run `agentic-os permissions init` (creates the local profile and read grants; add "
             "`--include-write` for write tools) and restart the app")


class GateStartupError(RuntimeError):
    """Permissions were requested and the gate could not be installed: the app must not start (fail closed)."""


class EnterpriseCapabilityMissing(GateStartupError):
    """An ENTERPRISE capability (SSO / tenant identity / advanced permissioning / permission-aware retrieval and cache lifecycle) was requested
    and the enterprise package is not installed. Names the capability and the fix."""


def _switch(env, name: str) -> str:
    """``"on"`` / ``"off"`` / ``"invalid"`` for a permission switch variable. Case-insensitive, surrounding white space ignored; on = 1, true, yes, on; off = 0,
    false, no, off or empty / unset; ANYTHING else is ``"invalid"`` (never read as off: a typo must not start an app ungated)."""
    v = str(env.get(name, "")).strip().lower()
    return "on" if v in _ON else "off" if v in _OFF else "invalid"


def _check_switches(env) -> None:
    for name in (BASIC_SWITCH, RETRIEVAL_SWITCH, CACHE_SWITCH, REQUIRE_SETTING):
        if _switch(env, name) == "invalid":
            raise GateStartupError(
                f"{name}={str(env.get(name))!r} is not a recognised value: use one of 1, true, yes, on (to turn it on) or 0, false, no, off (or leave it unset or empty) "
                f"(case-insensitive). The app refuses to start rather than run ungated on a value it cannot read.")


def basic_enabled(env=None) -> bool:
    """The kernel-owned switch for BASIC (AGPL) permissions: ``AGENTIC_OS_PERMISSIONS`` = 1 / true / yes / on. Any unrecognised non-empty value raises
    ``GateStartupError`` (``install_gate`` refuses to start); ``permissions_requested`` reads such a value as REQUESTED at call time."""
    env = os.environ if env is None else env
    _check_switches(env)
    return _switch(env, BASIC_SWITCH) == "on"


def enterprise_requests(env=None) -> list:
    """The ENTERPRISE capabilities the environment asks for, each as a (capability, variable) pair: the ``CR_PERMISSIONS`` bootstrap (any non-off
    value), enterprise identity (any ``CR_IDENTITY_*``, including ``CR_IDENTITY_MODE``), permission-aware retrieval and cache lifecycle."""
    env = os.environ if env is None else env
    out = []
    if str(env.get("CR_PERMISSIONS", "")).strip().lower() not in _OFF:
        out.append(("enterprise permissioning bootstrap (tenant authorization, advanced permissions)", "CR_PERMISSIONS"))
    idv = sorted(k for k, v in env.items() if k.startswith(IDENTITY_PREFIX) and str(v).strip() != "")
    if idv:
        out.append(("enterprise SSO / tenant identity", ", ".join(idv)))
    if _switch(env, RETRIEVAL_SWITCH) == "on":
        out.append(("enterprise permission-aware retrieval", RETRIEVAL_SWITCH))
    if _switch(env, CACHE_SWITCH) == "on":
        out.append(("enterprise permission-aware cache lifecycle", CACHE_SWITCH))
    return out


def enterprise_requested(env=None) -> bool:
    return bool(enterprise_requests(env))


def permissions_requested(env=None) -> bool:
    """True when basic OR enterprise permissions were requested: the app then starts gated or not at all (never silently open). An unrecognised value of a
    switch counts as REQUESTED here (install_gate refuses it at start-up; a console that still runs denies)."""
    env = os.environ if env is None else env
    if any(_switch(env, n) != "off" for n in (BASIC_SWITCH, RETRIEVAL_SWITCH, CACHE_SWITCH)):
        return True
    return enterprise_requested(env)


REQUIRE_SETTING = "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING"
_NOTICED: set = set()               # apps whose "authorization is not enforced" notice has been logged
_PROCESS_AUTHORIZER = None          # the kernel's own process-wide authorizer (basic mode)
_GATES: dict = {}                   # app name -> AppGate (basic mode)
_ENTERPRISE: dict = {}              # app name -> (principal_for, tool table) once the enterprise bootstrap installed its gate for it
_REQUEST: contextvars.ContextVar = contextvars.ContextVar("agentic_os_app_gate_request", default=None)


def reset_state() -> None:
    """Clear the process-wide gate state (the gates, the enterprise gates, the "not enforced" notices and the process authorizer), in place so every importer sees the reset. For tests and for a
    harness that re-imports apps in one process; production never needs it (``install_gate`` starts each app from a clean entry)."""
    global _PROCESS_AUTHORIZER
    _GATES.clear()
    _ENTERPRISE.clear()
    _NOTICED.clear()
    _PROCESS_AUTHORIZER = None


class RequestContext:
    """Pure ASGI middleware: makes the current request visible to ``tool_denial`` (through a context variable, which ``anyio`` copies into the thread pool that runs
    synchronous routes), so EVERY route can ask the active gate (basic or enterprise) with THIS request's principal without every signature carrying the request.
    Every app adds it right after the route-authorisation layer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        token = _REQUEST.set(Request(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST.reset(token)


class _EngineLayoutError(RuntimeError):
    """The engine is importable but the place it keeps its process-wide authorizer cannot be found."""


_NOT_FOUND = object()
_PACKAGES = ("context_runtime_enterprise", "context_runtime", "agent_harness")      # what the kernel may import on behalf of the enterprise mode / the engine hook


def _trusted_engine() -> bool:
    """False when the engine (``context_runtime``) is not importable from an installed distribution (absent, or present but untrusted); the kernel never imports it then (DR-02)."""
    return trusted_import.package_status("context_runtime") == "trusted"


def _untrusted_packages() -> list:
    """The enterprise / engine packages that are PRESENT but not provided by an installed distribution (a planted directory on ``sys.path`` executes when imported). Nothing is executed to find out."""
    return [n for n in _PACKAGES if trusted_import.package_status(n) == "untrusted"]


def _untrusted_text(names) -> str:
    return (f"{', '.join(names)} is present on sys.path but not provided by an installed distribution (a directory or file placed on sys.path would execute when imported, e.g. the "
            f"working directory of `python -m uvicorn`); it was not imported. Install the package properly (pip) or remove the stray path entry")


def _engine_default(strict: bool = False):
    """The engine's process-wide authorizer, looked up where the ENGINE keeps it, whatever the layout: a public getter on ``context_runtime.tools.base``
    (``get_default_authorizer``) if there is one; else the module-level ``_default_authorizer`` of ``base`` itself; else the same variable in the module
    that DEFINES ``base.set_default_authorizer`` (the real engine re-exports the setter from ``agent_harness.tools``, which holds the variable). Returns
    None when the engine is not importable or nothing is installed; when the engine is importable but none of those places exists: None, or with
    ``strict`` an ``_EngineLayoutError`` that names what was looked for (never a silent 'not installed')."""
    if _trusted_engine() is False:               # not installed, or present but not provided by an installed distribution (never imported: DR-02)
        return None
    try:
        base = importlib.import_module("context_runtime.tools.base")
    except Exception:  # noqa: BLE001 — the engine is not installed
        return None
    getter = getattr(base, "get_default_authorizer", None)
    if callable(getter):
        return getter()
    if hasattr(base, "_default_authorizer"):
        return base._default_authorizer
    setter = getattr(base, "set_default_authorizer", None)
    home = getattr(setter, "__module__", None)
    if home and home != base.__name__:
        try:
            owner = importlib.import_module(home) if trusted_import.package_status(home.split(".")[0]) == "trusted" else None
        except Exception:  # noqa: BLE001
            owner = None
        if owner is not None and hasattr(owner, "_default_authorizer"):
            return owner._default_authorizer
    if strict:
        raise _EngineLayoutError(
            "cannot read the engine's default authorizer back: context_runtime.tools.base has no get_default_authorizer() and no _default_authorizer, and "
            f"the module that defines set_default_authorizer ({home or 'unknown'}) has no _default_authorizer either (an unsupported engine layout)")
    return _NOT_FOUND


def _cr_default_authorizer():
    value = _engine_default(False)
    return None if value is _NOT_FOUND else value


def _cr_default_authorizer_strict():
    """As ``_cr_default_authorizer`` but an engine whose authorizer cannot be read back raises ``_EngineLayoutError`` instead of reading as 'none installed'."""
    value = _engine_default(True)
    return None if value is _NOT_FOUND else value


def default_authorizer():
    """The process-wide authorizer, read at call time. PRECEDENCE (FR-10): when BOTH the engine's default (what the enterprise bootstrap installs, or anything that
    called ``set_default_authorizer``) and the kernel's own (basic mode) are installed, the answer is the STRICTER of the two: a call is allowed only when BOTH allow
    (the first deny reason wins), so a permissive default installed later can never loosen basic mode and basic mode can never loosen the enterprise gate. One of
    them alone is returned as is; neither is None."""
    engine, kernel = _cr_default_authorizer(), _PROCESS_AUTHORIZER
    if engine is None or kernel is None or engine is kernel:
        return engine or kernel

    def both(principal, spec, args):
        return kernel(principal, spec, args) or engine(principal, spec, args)
    return both


def _engine_importable() -> bool:
    if _trusted_engine() is False:
        return False
    try:
        importlib.import_module("context_runtime.tools.base")
    except Exception:  # noqa: BLE001
        return False
    return True


def _ungated(request=None):
    """The principal of an app that runs without the enterprise gate: none (the basic default)."""
    return None


def _refuse(app_name: str, why: str, cause: BaseException | None = None, *, kind: str = "enterprise") -> GateStartupError:
    mode = ("basic permissions (AGENTIC_OS_PERMISSIONS)" if kind == "basic"
            else "enterprise permissions (CR_PERMISSIONS or CR_IDENTITY_* is set)")
    err = GateStartupError(
        f"app {app_name!r} cannot start: permissions were requested ({mode}) but the gate was not installed: "
        f"{why}. The app refuses to run ungated. Fix the cause, or unset the variables to run the basic default.")
    err.__cause__ = cause
    return err


def _missing_capability(app_name: str, requests: list, cause: BaseException | None) -> EnterpriseCapabilityMissing:
    caps = "; ".join(f"{c} (requested by {v})" for c, v in requests)
    err = EnterpriseCapabilityMissing(
        f"app {app_name!r} cannot start: enterprise capability unavailable: {caps}. The enterprise package `context_runtime_enterprise` is not "
        f"installed (not importable: {cause}). Fix: install the enterprise package, or unset those variables and use the BASIC permissions of this "
        f"repository instead (set {BASIC_SWITCH}=1 and run `agentic-os permissions init`). The app refuses to run ungated.")
    err.__cause__ = cause
    return err


# ───────────────────────── basic (AGPL) permissions ─────────────────────────

class AppGate:
    """One app's basic-permissions gate: the kernel authorizer over its resolved tool table and the LOCAL principal (never a request header,
    no roles, no admin bypass). ``restricted`` = no grants are provisioned yet: everything protected is denied with the ``init`` hint."""

    def __init__(self, app, authorizer, principal, table, *, restricted, directory, plane=None):
        self.app, self.authorizer, self.principal, self.table = app, authorizer, principal, dict(table)
        self.restricted, self.directory, self.plane = restricted, directory, plane

    def scope(self, tool: str):
        """The effective AUTHORISATION SCOPE of the current principal for ``tool``: resource, action and a digest of the CURRENT decision (allowed, row filter, column masks, per-grant
        rules). None when there is no plane (restricted) or the decision cannot be computed. A cached result is valid only within the scope it was produced under."""
        if self.plane is None:
            return None
        import hashlib
        import json
        action = self.table.get(tool, "read")
        resource = f"{self.app}.{tool}"
        try:
            d = self.plane.authorize(self.principal, "tool", resource, action)
            body = json.dumps({"allowed": d.allowed, "row_filter": d.row_filter, "masked": d.masked_columns, "rules": d.rules}, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return None
        return {"principal": getattr(self.principal, "user", ""), "resource": resource, "action": action,
                "digest": hashlib.sha256(body.encode("utf-8")).hexdigest()}

    def check(self, tool: str, principal=None) -> str | None:
        """The deny reason for running ``tool`` (None = allowed). A tool that is not in the app's table is denied."""
        spec = SimpleNamespace(name=tool, side_effecting=self.table.get(tool) in ("write", "delete"), approval_required=False)
        try:
            return self.authorizer(self.principal if principal is None else principal, spec, {})
        except Exception as e:  # noqa: BLE001 — fail closed
            return f"authorizer error ({type(e).__name__})"


def gate_for(app_name: str):
    return _GATES.get(app_name)


def _enterprise_denial(app_name: str, tool: str):
    principal_for, table = _ENTERPRISE[app_name]
    auth = _cr_default_authorizer()
    if auth is None:
        return "enterprise permissions are on but no authorizer is installed; denied (fail closed)"
    spec = SimpleNamespace(name=tool, side_effecting=table.get(tool) in ("write", "delete"), approval_required=False)
    try:
        # the principal of THIS request (the bootstrap resolves it from the verified credential); no request (a background job) = the anonymous principal
        return auth(principal_for(_REQUEST.get()), spec, {})
    except Exception as e:  # noqa: BLE001 — fail closed
        return f"authorizer error ({type(e).__name__})"


def tool_denial(app_name: str, tool: str) -> str | None:
    """The deny reason for executing ``tool`` of ``app_name`` under the ACTIVE gate, or None: basic permissions (the local profile principal), or the ENTERPRISE
    gate (the principal of the current request, anonymous when there is none), whichever the app was started with; the basic default (neither) allows."""
    gate = _GATES.get(app_name)
    basic = None if gate is None else gate.check(tool)
    if app_name in _ENTERPRISE:                      # an app is in one registry (install_gate clears both first); if a stale entry ever left it in both, the stricter answer wins
        return basic or _enterprise_denial(app_name, tool)
    return basic


def require_tool(app_name: str, tool: str) -> None:
    """Call where a tool EXECUTES: raises HTTP 403 when basic permissions deny it. A no-op when basic permissions are not on."""
    deny = tool_denial(app_name, tool)
    if deny:
        from fastapi import HTTPException
        raise HTTPException(403, f"[blocked] {tool}: {deny}")


def guarded(app_name: str, tool: str):
    """THE shared execution wrapper (one for all the apps that dispatch tool bodies from routes): decorate the function that RUNS a tool. It asks the active gate for ``tool`` before the body and
    returns the documented denial dict (status denied, executed False, the actionable reason) instead of running it, so no route, console intent, job or direct call can reach the body without the
    gate. A no-op when permissions are not requested. The wrapped function carries ``__guarded_tool__ = (app, tool)`` (a test walks the apps for it)."""
    def deco(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(*a, **k):
            denied = denial_result(app_name, tool)
            return denied if denied is not None else fn(*a, **k)
        wrapper.__guarded_tool__ = (app_name, tool)
        return wrapper
    return deco


def deny_response(app_name: str, *tools: str):
    """A 403 JSON response when basic permissions deny ANY of ``tools`` (every one is required), else None. Call at the head of a route that
    executes a tool: ``if (r := deny_response(APP, "x.y")): return r``. The body says denied / executed False and carries the actionable reason."""
    for tool in tools:
        deny = tool_denial(app_name, tool)
        if deny:
            from fastapi.responses import JSONResponse
            return JSONResponse({"status": "denied", "executed": False, "error": f"[blocked] {tool}: {deny}"}, status_code=403)
    return None


def denial_result(app_name: str, *tools: str):
    """The same as ``deny_response`` for code that is not a route (a console intent, a background job): a plain dict, or None."""
    for tool in tools:
        deny = tool_denial(app_name, tool)
        if deny:
            return {"status": "denied", "executed": False, "error": f"[blocked] {tool}: {deny}"}
    return None


# Operators of the kernel's own planes are governed by those planes (the customer-interaction layer; the Agent Gateway's social and external-agent operators, behind the gateway's token auth, capability
# filtering and approvals), not by an app's tool table. That exemption is trusted metadata about a specific OPERATOR OBJECT and its specific CAPABILITIES, registered by the three kernel modules that build
# them, never a property of an operator NAME (DR-04): an operator someone calls "social" is gated like any other. ``kernel_governed`` refuses any other caller.
KERNEL_GOVERNED_MODULES = frozenset({"agentic_os.agent_gateway.social.operator", "agentic_os.agent_gateway.external.operator", "agentic_os.interaction.operator"})
_KERNEL_GOVERNED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()      # Operator object -> {capability name: why a kernel plane governs it}


def kernel_governed(operator, reason: str) -> None:
    """Register the capabilities ``operator`` has RIGHT NOW as governed by a kernel plane (exempt from the app tool-table seam). Only the three kernel plane modules may call it: the caller's module is
    checked against ``KERNEL_GOVERNED_MODULES``, and a capability added to the operator afterwards is not covered."""
    caller = sys._getframe(1).f_globals.get("__name__", "")
    if caller not in KERNEL_GOVERNED_MODULES:
        raise GateStartupError(f"{caller or 'this code'} cannot exempt an operator from the permissions seam: only the kernel plane modules ({', '.join(sorted(KERNEL_GOVERNED_MODULES))}) govern operators themselves")
    if not str(reason or "").strip():
        raise GateStartupError("a kernel-plane exemption needs the reason the plane governs these capabilities")
    _KERNEL_GOVERNED[operator] = {c.name: reason for c in operator.manifest.capabilities}


# ───────────────────────── public intake (a constrained exception) ─────────────────────────

class _Seal:
    def __repr__(self) -> str:
        return "<public-intake seal>"


_SEAL = _Seal()


@dataclass(frozen=True)
class PublicIntake:
    """A ticket for ONE registered public route (``tool_actions.PUBLIC_INTAKE_ROUTES``): it lets that route create a PENDING intake request for the one tool the registry names, and nothing else. It is not an
    argument of the Operator and no grant stands behind it; it is minted only by ``public_intake`` and recognised only by ``is_public_intake`` (the seal is private to this module)."""
    app: str
    method: str
    path: str
    tool: str
    seal: object = None


def public_intake(app_name: str, method: str, path: str) -> PublicIntake:
    """The ticket for a public route: raises ``GateStartupError`` unless (app, METHOD, path) is a route registered in trusted code, and unless the tool it names is still classified ``write``."""
    from .tool_actions import PUBLIC_INTAKE_ROUTES, TOOL_ACTIONS
    tool = PUBLIC_INTAKE_ROUTES.get(app_name, {}).get((method, path)) if isinstance(app_name, str) and isinstance(method, str) and isinstance(path, str) else None
    if tool is None:
        raise GateStartupError(f"{method} {path} of {app_name!r} is not a registered public-intake route (agentic_os.tool_actions.PUBLIC_INTAKE_ROUTES); nothing is exempt from the permissions gate by being "
                               "called a public route")
    if TOOL_ACTIONS.get(app_name, {}).get(tool) != "write":
        raise GateStartupError(f"public-intake tool {tool!r} of {app_name!r} must stay classified write for every ordinary interface")
    return PublicIntake(app_name, method, path, tool, _SEAL)


def is_public_intake(ticket, app_name: str, tool: str) -> bool:
    """True only for a genuine ticket, minted for ``app_name`` and exactly ``tool`` and still a registered route."""
    from .tool_actions import PUBLIC_INTAKE_ROUTES
    return (isinstance(ticket, PublicIntake) and ticket.seal is _SEAL and ticket.app == app_name and ticket.tool == tool
            and PUBLIC_INTAKE_ROUTES.get(ticket.app, {}).get((ticket.method, ticket.path)) == tool)


def _digest(obj) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _enterprise_principal(operator_name: str):
    principal_for, _table = _ENTERPRISE[operator_name]
    try:
        return principal_for(_REQUEST.get())
    except Exception:  # noqa: BLE001 — a resolver that raises identifies nobody: the key of the anonymous principal
        return None


def _enterprise_principal_key(operator_name: str) -> str:
    """A digest of WHO the enterprise gate sees for this request: user, app, the role SET (never the raw identity in a cache key)."""
    p = _enterprise_principal(operator_name)
    roles = getattr(p, "roles", None)
    try:
        role_set = sorted(str(r) for r in roles) if roles is not None else []
    except TypeError:
        role_set = [repr(roles)]
    return "ent:" + _digest({"user": str(getattr(p, "user", "")) if p is not None else "", "app": str(getattr(p, "app", "")) if p is not None else "", "roles": role_set,
                             "type": type(p).__name__})[:32]


def _enterprise_scope(operator_name: str, capability: str) -> str:
    """The digest of the CURRENT scope the enterprise gate grants this request's principal for a capability: the plane's decision (allow with its row filter, column masks and per-grant rules, or the
    deny reason) when the installed authorizer can describe it (``decide``), else the allow/deny outcome of ``check`` alone (still principal-keyed). It changes whenever the grants change."""
    _principal_for, table = _ENTERPRISE[operator_name]
    auth = _cr_default_authorizer()
    p = _enterprise_principal(operator_name)
    spec = SimpleNamespace(name=capability, side_effecting=table.get(capability) in ("write", "delete"), approval_required=False)
    body: dict = {"check": None}
    try:
        decide = getattr(getattr(auth, "__self__", None), "decide", None)
        if callable(decide):
            d = decide(p, capability, spec)
            body["decision"] = d if isinstance(d, str) else {"allowed": getattr(d, "allowed", None), "row_filter": getattr(d, "row_filter", None),
                                                               "masked": getattr(d, "masked_columns", None), "rules": getattr(d, "rules", None), "reason": getattr(d, "reason", "")}
        body["check"] = None if auth is None else auth(p, spec, {})
    except Exception as e:  # noqa: BLE001 — undescribable: the type of the failure, which is itself a scope (denied)
        body["error"] = type(e).__name__
    return f"{_enterprise_principal_key(operator_name)}|{capability}|{_digest(body)}"


def operator_scope(operator_name: str, capability: str) -> str:
    """The digest of the current authorisation scope of an operator's principal for a capability: the basic gate's decision for the local profile principal, or the enterprise gate's decision for the
    principal of THIS request ("" when there is no gate or no plane)."""
    gate = _GATES.get(operator_name)
    if gate is None:
        return _enterprise_scope(operator_name, capability) if operator_name in _ENTERPRISE else ""
    s = gate.scope(capability)
    return "" if s is None else f"{s['principal']}|{s['resource']}|{s['action']}|{s['digest']}"


def principal_key(operator_name: str) -> str:
    """The principal an operator's calls run as, part of the idempotency key so two principals never share a cached result: the local profile user (basic mode), or a digest of the identity the
    enterprise gate resolves for THIS request (enterprise mode)."""
    gate = _GATES.get(operator_name)
    if gate is None:
        return _enterprise_principal_key(operator_name) if operator_name in _ENTERPRISE else ""
    return str(getattr(gate.principal, "user", ""))


def _operator_basic_denial(operator_name: str, capability: str) -> str | None:
    gate = _GATES.get(operator_name)
    return None if gate is None else gate.check(capability)


def operator_denial(operator_name: str, capability: str, operator=None) -> str | None:
    """The Operator SDK's execution seam (``Operator._execute``): the deny reason for a capability, or None.

    FAILS CLOSED (FR-01): when basic permissions are requested (an unrecognised switch value counts as requested) and NO gate is registered under this
    operator's name, the capability is denied. A process that builds operators without calling ``install_gate`` (the suite server, sky, video-ad, zoning, a test
    harness) must never run them ungated just because nobody installed a gate. The operators the kernel planes build are exempt PER CAPABILITY by registration (``kernel_governed``) of the operator object, never by name."""
    gate = _GATES.get(operator_name)
    if gate is not None:
        return gate.check(capability) or (_enterprise_denial(operator_name, capability) if operator_name in _ENTERPRISE else None)       # in both registries (stale): the stricter wins
    if operator is not None and capability in _KERNEL_GOVERNED.get(operator, ()):
        return None
    if operator_name in _ENTERPRISE:                 # the enterprise gate is installed for this operator: evaluate it with THIS request's principal (HTTP /invoke), anonymous in-process
        return _enterprise_denial(operator_name, capability)
    if not permissions_requested():                  # (a) not requested: the basic default / explicit standalone policy
        return None
    if enterprise_requested():
        return (f"enterprise permissions are on but no permissions gate is installed for operator {operator_name!r}: the process that built it must call "
                f"agentic_os.app_gate.install_gate({operator_name!r}); denied (fail closed)")
    return (f"basic permissions are on but no permissions gate is installed for operator {operator_name!r}: the process that built it must call "
            f"agentic_os.app_gate.install_gate({operator_name!r}) (it denies until it does). If this is an app, start it through its app module; "
            f"{INIT_HINT.split(':')[0]}")


def _provisioned(directory: str, env) -> bool:
    """Is a store key provisioned (env key, or a key file)? No key = no grants provisioned = start restricted."""
    if str(env.get("PERMISSIONS_KEY", "")).strip():
        return True
    kf = env.get("PERMISSIONS_KEY_FILE") or os.path.join(directory, ".permissions.key")
    return os.path.exists(kf)


def _install_basic(app_name: str, env):
    from .permissions import GrantStore, Identity, PermissionsPlane, make_authorizer
    from .permissions_setup import default_dir, local_user
    from .tool_actions import resolve_tool_actions
    global _PROCESS_AUTHORIZER
    try:
        table = resolve_tool_actions(app_name, enterprise=False) or {}          # the kernel's table; basic permissions never read the enterprise one
    except Exception as e:  # noqa: BLE001 — a conflicting or unreadable table is a real failure, not "no grants"
        raise _refuse(app_name, f"its tool classification could not be resolved ({type(e).__name__}: {e})", e, kind="basic") from e
    directory = env.get("PERMISSIONS_DIR") or default_dir()
    if _provisioned(directory, env):
        try:
            store = GrantStore(directory)
            user = local_user(directory)
            inner = make_authorizer(store, tool_resources={t: ("tool", f"{app_name}.{t}") for t in table}, tool_actions=table, privileged_roles=())
        except Exception as e:  # noqa: BLE001 — the key IS provisioned, so a store that cannot open is a failure, not "no grants"
            raise _refuse(app_name, f"the grant store or the local profile could not be opened ({type(e).__name__}: {e})", e, kind="basic") from e
        principal, restricted = Identity(app="", user=user, roles=[]), False

    else:
        def inner(principal, spec, args):
            return "no permissions are provisioned"
        principal, restricted = Identity(app="", user="", roles=[]), True
        _log.warning("app %r: basic permissions are on but no permissions key is provisioned: the app starts RESTRICTED and denies every protected "
                     "operation until `agentic-os permissions init` is run", app_name)

    def authorizer(principal, spec, args):
        try:
            reason = inner(principal, spec, args)
        except Exception as e:  # noqa: BLE001 — fail closed
            reason = f"authorizer error ({type(e).__name__})"
        return None if reason is None else f"{reason} ({INIT_HINT})"

    gate = AppGate(app_name, authorizer, principal, table, restricted=restricted, directory=directory,
                   plane=None if restricted else PermissionsPlane(store, privileged_roles=()))
    _GATES[app_name] = gate
    _PROCESS_AUTHORIZER = authorizer
    if _trusted_engine():                                    # the harness engine (public package, only from an installed distribution) reads its own default: same authorizer
        try:
            from context_runtime.tools import base
            base.set_default_authorizer(authorizer)
        except ImportError:
            pass
    elif trusted_import.package_status("context_runtime") == "untrusted":
        _log.warning("app %r: %s; basic permissions do not need it and it was skipped", app_name, _untrusted_text(["context_runtime"]))

    def principal_for(request=None):
        return gate.principal                                # the local profile, whatever the request carries

    principal_for.gate = gate
    principal_for.mode = "basic"
    return principal_for


# ───────────────────────── enterprise bootstrap (requested) / basic default ─────────────────────────

def _install_enterprise(app_name: str, open_tools, requests: list):
    """Install the ENTERPRISE gate. Called ONLY when an enterprise capability was explicitly requested (``CR_PERMISSIONS``, ``CR_IDENTITY_*``, the permission-aware switches): nothing else ever
    imports ``context_runtime_enterprise`` (DR-02), and even then only after the package and the engine are verified to come from an installed distribution (``trusted_import``)."""
    stray = _untrusted_packages()
    if stray:                                  # never import the enterprise package or the engine from sys.path unless an installed distribution provides it
        raise _refuse(app_name, _untrusted_text(stray))
    try:
        apps = importlib.import_module("context_runtime_enterprise.apps")
    except ImportError as e:
        raise _missing_capability(app_name, requests, e) from e
    bootstrap = getattr(apps, "bootstrap", None)
    if not callable(bootstrap):
        raise _refuse(app_name, "context_runtime_enterprise.apps has no bootstrap()")

    kwargs: dict = {}
    try:
        from agentic_os.tool_actions import resolve_tool_actions
        actions = resolve_tool_actions(app_name, enterprise=True)            # the enterprise table is cross-checked only in the enterprise mode
    except Exception as e:  # noqa: BLE001 — a table that cannot be resolved is a start-up error
        raise _refuse(app_name, f"its tool classification could not be resolved ({type(e).__name__}: {e})", e) from e
    if actions is None:
        raise _refuse(app_name, "it has no tool classification table (neither agentic_os.tool_actions nor the enterprise "
                                "fleet_tool_actions classifies this app; bootstrap would raise ToolActionsRequired)")
    try:
        params = inspect.signature(bootstrap).parameters
        takes = "tool_actions" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        takes = False
    if not takes:
        raise _refuse(app_name, "the installed enterprise bootstrap() does not accept tool_actions (too old to enforce action classes)")
    kwargs["tool_actions"] = actions
    if open_tools:
        kwargs["open_tools"] = tuple(open_tools)

    try:
        principal_for = bootstrap(app_name, **kwargs)
    except Exception as e:  # noqa: BLE001
        raise _refuse(app_name, f"bootstrap raised {type(e).__name__}: {e}", e) from e
    if not callable(principal_for):
        raise _refuse(app_name, "bootstrap() returned no principal resolver")
    _ENTERPRISE[app_name] = (principal_for, dict(actions or {}))              # routes ask THIS gate with the request's principal (KD-3)
    if _engine_importable():
        try:
            installed = _cr_default_authorizer_strict()
        except _EngineLayoutError as e:
            raise _refuse(app_name, str(e), e) from e
        if installed is None:
            raise _refuse(app_name, "bootstrap() returned but no process-wide authorizer is installed")
    return principal_for


def _unconfigured_or_standalone(app_name: str, env) -> None:
    """Nothing is requested. UNSET is not consent: say so loudly (or refuse, with ``AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1``); an explicit off is the deliberate standalone mode, bannered."""
    explicit = str(env.get(BASIC_SWITCH, "")).strip() != ""
    if explicit:                                                          # an explicit off spelling (an invalid one was refused earlier)
        if app_name not in _NOTICED:
            _NOTICED.add(app_name)
            _log.warning("app %r: authorization is explicitly disabled (%s=%s): authorization is DISABLED, standalone mode, no grant is enforced on its tools; the app reports "
                         "`authorization: unenforced` on /health. Never expose it to a network you do not control; to enforce grants run `agentic-os permissions init` and set %s=1.",
                         app_name, BASIC_SWITCH, env.get(BASIC_SWITCH), BASIC_SWITCH)
        return
    if _switch(env, REQUIRE_SETTING) == "on":
        raise GateStartupError(
            f"app {app_name!r} refuses to start: authorization is neither enabled nor explicitly disabled ({BASIC_SWITCH} is unset and {REQUIRE_SETTING}=1). Set "
            f"{BASIC_SWITCH}=1 (after `agentic-os permissions init`) to enforce grants, or {BASIC_SWITCH}=off to run deliberately without authorization (standalone).")
    if app_name not in _NOTICED:
        _NOTICED.add(app_name)
        _log.warning("app %r: AUTHORIZATION IS NOT ENFORCED: %s is unset, so every tool of this app runs for anyone who can reach it (the basic default); /health reports "
                     "`authorization: unenforced`. For a deployment that is reachable from a network set %s=1 (after `agentic-os permissions init`; the enterprise gate: CR_PERMISSIONS=1 with the "
                     "enterprise package) AND %s=1, so that an unset setting refuses to start. To run without authorization on purpose set %s=off (still unenforced, still reported). Run "
                     "directly on a loopback address this is only the single-user quickstart.", app_name, BASIC_SWITCH, BASIC_SWITCH, REQUIRE_SETTING, BASIC_SWITCH)


def authorization_fields(app_name: str, env=None) -> dict:
    """What an app reports about its authorization on ``/health`` and ``/_security``: ``authorization`` is ``enforced`` (a gate decides, grants provisioned), ``restricted`` (permissions are
    requested but no permissions key is provisioned or no gate is installed: everything protected is denied) or ``unenforced`` (nothing requested, or deliberately off: every tool runs for anyone who
    can reach the app); ``authorization_setting`` says why (``on``, ``off``, ``unset``)."""
    envm = os.environ if env is None else env
    gate = _GATES.get(app_name)
    if gate is not None:
        return {"authorization": "restricted" if gate.restricted else "enforced", "authorization_setting": "on"}
    if app_name in _ENTERPRISE:
        return {"authorization": "enforced", "authorization_setting": "enterprise"}
    try:
        requested = permissions_requested(envm)
    except GateStartupError:
        requested = True
    if requested:
        return {"authorization": "restricted", "authorization_setting": "on"}
    return {"authorization": "unenforced", "authorization_setting": "off" if str(envm.get(BASIC_SWITCH, "")).strip() != "" else "unset"}


def with_authorization(app_name: str, payload: dict) -> dict:
    """``payload`` plus the authorization fields (an app's ``/health`` answer)."""
    return {**payload, **authorization_fields(app_name)}


def require_explicit_setting_when_exposed(host: str, env=None) -> None:
    """For code that binds a socket ITSELF (an app's ``__main__``): a reachable (non-loopback) bind with no explicit permissions setting is refused. Loopback is the local single-user dev path."""
    env = os.environ if env is None else env
    if host in ("127.0.0.1", "localhost", "::1") or permissions_requested(env):
        return
    if str(env.get(BASIC_SWITCH, "")).strip() != "":
        return
    raise SystemExit(f"refusing to listen on {host}: authorization is neither enabled nor explicitly disabled. Set {BASIC_SWITCH}=1 (after `agentic-os permissions init`) to enforce grants, "
                     f"or {BASIC_SWITCH}=off to run deliberately without authorization; bind 127.0.0.1 for local single-user use.")


def install_gate(app_name: str, *, open_tools=(), env=None):
    """Join a permissions plane and return ``principal_for(request)``; see the module docstring for the table and the fail-closed rule."""
    envm = os.environ if env is None else env
    _check_switches(envm)                          # an unrecognised switch value stops the app (FR-03), never starts it ungated
    global _PROCESS_AUTHORIZER
    _GATES.pop(app_name, None)                     # (re)installing starts from a clean state for this app
    _ENTERPRISE.pop(app_name, None)
    requests = enterprise_requests(envm)
    if not _GATES and not requests:
        _PROCESS_AUTHORIZER = None                 # no basic gate is left and none is being installed for the enterprise mode: a stale process-wide authorizer (an older store, an older profile) must not outlive them
    if requests:                                   # ENTERPRISE requested: the package or a clear missing-capability error
        return _install_enterprise(app_name, open_tools, requests)
    if basic_enabled(envm):                        # BASIC (AGPL): the kernel's own plane, no enterprise package needed
        return _install_basic(app_name, envm)
    _unconfigured_or_standalone(app_name, envm)
    if os.environ.get("CR_PERMISSIONS_DEV_UNGATED_ACK"):
        _log.warning("DEVELOPMENT ONLY — NOT FOR PRODUCTION: CR_PERMISSIONS_DEV_UNGATED_ACK is set and no permissions were requested, so app %r runs with NO tool authorization "
                     "(the basic default). Never set this in a shipped configuration.", app_name)
    return _ungated                                # NOTHING requested: the enterprise package is not imported at all (DR-02), whether or not it is installed
