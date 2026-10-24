"""CI gate for F20: every state-changing route an app serves has an authorization policy, and no policy is stale.

This imports each real app, asks FastAPI which write routes (and WebSocket routes) it actually registered, and requires an
exact match with the app's `route_policy.ROUTE_POLICIES`. A new route added without a policy fails here; in the production
profile the same route is also refused at runtime."""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest

from agentic_os.app_kit.route_authz import ACCESS, WRITE_METHODS, RoutePolicy, unclassified, write_routes

HERE = Path(__file__).parent
APPS = sorted(p.parent.name for p in HERE.glob("*/route_policy.py"))


def _decorated_write_routes(app_dir: Path) -> list:
    out = []
    for path in sorted(app_dir.glob("*.py")):
        if path.name == "operator.py" or path.name.startswith("test_") or path.name == "seed.py":
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.args
                            and isinstance(d.args[0], ast.Constant)
                            and (d.func.attr in {m.lower() for m in WRITE_METHODS} or d.func.attr == "websocket")):
                        out.append(path.name)
    return out


def test_every_app_that_declares_write_routes_has_a_policy_file_and_installs_it():
    missing = []
    for app_dir in sorted(HERE.iterdir()):
        if not (app_dir / "app.py").exists() or app_dir.name.startswith("_"):
            continue
        has_routes = bool(_decorated_write_routes(app_dir))
        has_operator_mount = any("include_router(" in ln and ("operator" in ln or "secure_router" in ln)
                                 for ln in (app_dir / "app.py").read_text(encoding="utf-8").split("\n"))
        if (has_routes or has_operator_mount) and not (app_dir / "route_policy.py").exists():
            missing.append(app_dir.name)
        if (app_dir / "route_policy.py").exists() and "_route_authz.install(" not in (app_dir / "app.py").read_text(encoding="utf-8"):
            missing.append(f"{app_dir.name} (policy not installed)")
    assert not missing, f"write routes without an authorization policy: {missing}"


@pytest.mark.parametrize("pkg", APPS)
def test_declared_policies_match_the_routes_the_app_really_serves(pkg, load_app):
    app = load_app(pkg).app
    policies = importlib.import_module(f"{pkg}.route_policy").ROUTE_POLICIES
    assert unclassified(app, policies) == [], f"{pkg}: write routes with no policy"
    stale = sorted(set(policies) - set(write_routes(app)))
    assert stale == [], f"{pkg}: policies for routes that do not exist (stale): {stale}"


@pytest.mark.parametrize("pkg", APPS)
def test_policies_are_well_formed_and_installed(pkg, load_app):
    app = load_app(pkg).app
    pols = app.state.route_policies
    assert pols, f"{pkg}: route_authz.install was not applied"
    for key, pol in pols.items():
        assert isinstance(pol, RoutePolicy) and pol.access in ACCESS and pol.permission == f"{pkg}:write", key
        if pol.access == "public_intake":
            assert len(pol.rationale) > 30, f"{key}: an unauthenticated route needs a real rationale"
    assert any(type(m).__name__ == "Middleware" and getattr(m, "cls", None).__name__ == "RouteAuthzMiddleware"
               for m in app.user_middleware)


def test_unauthenticated_routes_are_few_and_named():
    """The set of routes that accept anonymous callers is explicit and reviewed, not an accident."""
    anon = sorted(f"{pkg} {key}" for pkg in APPS
                  for key, pol in importlib.import_module(f"{pkg}.route_policy").ROUTE_POLICIES.items()
                  if pol.access == "public_intake")
    assert anon == ["agentic-privacy POST /intake", "agentic-privacy POST /request", "agentic-privacy POST /verify"]
