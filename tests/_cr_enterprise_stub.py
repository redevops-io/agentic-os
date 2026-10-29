"""A STUB of the enterprise bootstrap and the context_runtime default-authorizer seam, for tests only.

``context_runtime`` and ``context_runtime_enterprise`` are optional packages that are not installed here, so the tests that need the apps' start-up path
use this stand-in. It reproduces the raise behaviour of the optional Enterprise package's bootstrap (its application, authorization and identity
modules), not its identity verification:

* ``CR_PERMISSIONS`` on and no ``tool_actions``        -> ``ToolActionsRequired`` BEFORE anything is installed;
* ``CR_IDENTITY_MODE`` not in ("", "token", "proxy")   -> ``IdentityConfigError`` BEFORE anything is installed;
* ``CR_PERMISSIONS`` on                                -> installs the process-wide authorizer (``set_default_authorizer``);
* ``CR_PERMISSIONS`` off                               -> installs a deny-all authorizer (off is not open), or lifts it with the dev ack.

The identity of a request is read from the test-only headers ``x-test-user`` / ``x-test-roles`` (a stand-in for a verified token).
"""
from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass, field

DEV_ACK = "i-understand-this-disables-all-tool-authorization"


class ToolActionsRequired(RuntimeError):
    pass


class IdentityConfigError(RuntimeError):
    pass


@dataclass
class Stub:
    modules: dict = field(default_factory=dict)
    base: types.ModuleType | None = None
    holder: types.ModuleType | None = None     # the module that actually holds ``_default_authorizer`` (the engine's layout decides which)
    bootstrap_calls: list = field(default_factory=list)
    fleet: dict | None = None            # fleet_tool_actions table, or None for "this enterprise build has no fleet table"
    factory: object = None               # tool_actions -> authorizer installed when permissions are on

    def default_authorizer(self):
        return getattr(self.holder, "_default_authorizer", None) if self.holder is not None else None


def _default_factory(tool_actions):
    def check(principal, spec, args):
        name = getattr(spec, "name", "")
        if name not in tool_actions:
            return f"tool {name!r} is not classified; denied (fail closed)"
        if getattr(principal, "user", "") != "granted":
            return f"principal {getattr(principal, 'user', None)!r} holds no grant for {name!r}"
        return None
    return check


def install(monkeypatch, *, fleet=None, factory=None, with_engine=True, with_fleet_module=True, bootstrap_has_tool_actions=True,
            layout="base", bootstrap_has_install=True) -> Stub:
    """``layout`` is where the engine keeps its process-wide authorizer: ``base`` (the variable lives in ``context_runtime.tools.base`` itself),
    ``core`` (``base`` re-exports the setter from another module, which holds the variable: the REAL engine keeps it in ``agent_harness.tools``),
    ``getter`` (a public ``get_default_authorizer`` on ``base``; the variable is private to the other module), ``unknown`` (a setter and nothing
    that can read it back)."""
    st = Stub(fleet=fleet, factory=factory or _default_factory)
    mods: dict[str, types.ModuleType] = {}

    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__path__ = []
        m.__dict__.update(attrs)
        mods[name] = m
        return m

    if with_engine:
        base = mod("context_runtime.tools.base")
        if layout == "base":
            base._default_authorizer = None
            base.set_default_authorizer = lambda fn: setattr(base, "_default_authorizer", fn)
            st.holder = base
        else:
            core = mod("fake_engine_core")
            slot = {"fn": None}

            def set_default_authorizer(fn):
                slot["fn"] = fn
                if layout == "core":
                    core._default_authorizer = fn            # the variable lives in the OTHER module (the real engine: agent_harness.tools)
            set_default_authorizer.__module__ = "fake_engine_core"
            base.set_default_authorizer = set_default_authorizer
            if layout == "core":
                core._default_authorizer = None
            elif layout == "getter":
                base.get_default_authorizer = lambda: slot["fn"]          # a public getter; the variable itself is not exposed anywhere
            st.holder = types.SimpleNamespace(_slot=slot)
            st.default_authorizer = lambda: (core._default_authorizer if layout == "core" else slot["fn"])        # this instance only
        st.base = base
        mod("context_runtime")
        mod("context_runtime.tools")
        mod("context_runtime.adapters")
        mod("context_runtime.adapters.model_openai",
            OpenAICompatibleModel=type("OpenAICompatibleModel", (), {"from_env": classmethod(lambda cls: None)}))
        mod("context_runtime.types", ModelRequest=type("ModelRequest", (), {"__init__": lambda self, *a, **k: None}))

    def enabled():
        return os.getenv("CR_PERMISSIONS", "").lower() in ("1", "true", "yes")

    def _principal_for(app_name):
        def principal_for(request=None):
            from agentic_os.permissions import Identity
            if request is None:
                return Identity(app=app_name, user="", roles=[])
            h = request.headers
            return Identity(app=app_name, user=h.get("x-test-user", ""), roles=[r for r in h.get("x-test-roles", "").split(",") if r])
        return principal_for

    def bootstrap_full(app_name, *, install=True, open_tools=(), tool_actions=None, identity=None, service_identity=None):
        st.bootstrap_calls.append({"app": app_name, "tool_actions": tool_actions, "open_tools": tuple(open_tools), "install": install})
        mode = os.getenv("CR_IDENTITY_MODE", "").strip().lower()
        if mode not in ("", "token", "proxy"):
            raise IdentityConfigError(f"CR_IDENTITY_MODE={mode!r} is not 'token' or 'proxy'")
        if install and st.base is not None:
            if enabled():
                if tool_actions is None:
                    raise ToolActionsRequired(f"app {app_name!r}: CR_PERMISSIONS is on but bootstrap() got no tool_actions")
                st.base.set_default_authorizer(st.factory(dict(tool_actions)))
            elif os.environ.get("CR_PERMISSIONS_DEV_UNGATED_ACK") == DEV_ACK:
                st.base.set_default_authorizer(None)
            else:
                reason = f"permissions are disabled for app {app_name!r}: every tool call is denied"
                st.base.set_default_authorizer(lambda principal, spec, args: reason)
        return _principal_for(app_name)

    def bootstrap_old(app_name, *, install=True, open_tools=()):
        return bootstrap_full(app_name, install=install, open_tools=open_tools, tool_actions=None) if not enabled() else \
            _principal_for(app_name)

    mod("context_runtime_enterprise")
    def bootstrap_no_install(app_name, *, open_tools=(), tool_actions=None, identity=None, service_identity=None):
        return bootstrap_full(app_name, install=True, open_tools=open_tools, tool_actions=tool_actions)

    mod("context_runtime_enterprise.apps",
        bootstrap=(bootstrap_full if bootstrap_has_install else bootstrap_no_install) if bootstrap_has_tool_actions else bootstrap_old)
    mod("context_runtime_enterprise.authz", ToolActionsRequired=ToolActionsRequired)
    if with_fleet_module:
        def fleet_tool_actions(app):
            if st.fleet is not None and app in st.fleet:
                return dict(st.fleet[app])
            raise ToolActionsRequired(f"app {app!r} has no enterprise classification")
        mod("context_runtime_enterprise.fleet_tool_actions", fleet_tool_actions=fleet_tool_actions)

    # the stand-in represents a PROPERLY INSTALLED package (a distribution provides it); the planted-module case has its own test
    from agentic_os import tool_actions as _ta
    monkeypatch.setattr(_ta, "_enterprise_package_trusted", lambda: True)
    from agentic_os import trusted_import as _ti
    _real_status, _tops = _ti.package_status, {n.split(".")[0] for n in mods}
    monkeypatch.setattr(_ti, "package_status", lambda top: "trusted" if top in _tops else _real_status(top))      # DR-02: the stand-in is a properly installed package
    for name in [n for n in list(sys.modules) if n == "context_runtime" or n.startswith(("context_runtime.", "context_runtime_enterprise"))]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name, m in mods.items():
        monkeypatch.setitem(sys.modules, name, m)
    st.modules = mods
    return st


def absent(monkeypatch) -> None:
    """The enterprise package is not installed: importing it raises ModuleNotFoundError (a ``None`` entry in sys.modules blocks the import)."""
    for name in ("context_runtime_enterprise", "context_runtime_enterprise.apps", "context_runtime_enterprise.fleet_tool_actions"):
        monkeypatch.setitem(sys.modules, name, None)
