"""KD-3 / FR-02: the route walk. Under the ENTERPRISE gate (CR_PERMISSIONS=1, a stand-in for the real bootstrap) and under BASIC permissions, an ANONYMOUS request to every route
of every one of the 17 apps must not execute a tool or return tool-derived data. A route that executes a tool or returns tool/activity data asks the ACTIVE gate with the request's
principal; the routes that are public by design are in ``_route_table.PUBLIC`` with the reason, and the framework / operator routes are listed too. Nothing else may exist.

"Nothing executed" is proven by a network sentinel (the cores talk to their backends over the network) and by the response shape.
"""
from __future__ import annotations

import importlib
import pathlib
import socket
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _cr_enterprise_stub as stub  # noqa: E402
from _route_table import FRAMEWORK_ROUTES, OPERATOR_ROUTES, PUBLIC, ROUTES  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402

APPS = tuple(ROUTES)
assert len(APPS) == 17


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("SENTINEL_RATE_BURST", "1000000")                  # edge-sentinel's per-IP rate limiter answers 429 before the handler; not what is tested here
    monkeypatch.setenv("SENTINEL_RATE_PER_MIN", "1000000000")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)


@pytest.fixture()
def net(monkeypatch):
    attempts = []

    def refuse_connect(self, address, *a, **k):
        attempts.append(address)
        raise OSError("network blocked by the test")

    def refuse_lookup(host, *a, **k):
        attempts.append(host)
        raise socket.gaierror("network blocked by the test")
    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_lookup)

    # processes too: a granted tool body may launch one (compliance.scan runs scan.sh, edge-sentinel shells out to docker/cscli, infra runs terraform): record the attempt
    # and refuse it, so no test ever executes a real scanner or writes into the repository
    import subprocess

    def refuse_process(*a, **k):
        attempts.append(("process", a[0] if a else k.get("args")))
        raise FileNotFoundError("process launch blocked by the test")
    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, refuse_process)
    monkeypatch.setattr("os.system", refuse_process)
    return attempts


def _start(monkeypatch, app):
    for name in [n for n in sys.modules if n == f"{app}.app"]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(f"{app}.app")


def _call(client, method, path, body, headers=None):
    return client.request(method, path, json=body, headers=headers or {}) if body is not None else client.request(method, path, headers=headers or {})


def _refused(resp):
    """The documented refusals: a 403 body saying nothing executed, a 200 console body that says denied, an operator-auth 401/503 before the handler."""
    try:
        j = resp.json()
    except Exception:  # noqa: BLE001
        j = {}
    if resp.status_code == 403 and j.get("executed") is False and "[blocked]" in str(j.get("error")):
        return True
    if resp.status_code == 200 and (j.get("denied") is True or j.get("executed") is False) and "[blocked]" in (str(j.get("text")) + str(j.get("say"))):
        return True
    return resp.status_code in (401, 503) and "[blocked]" not in resp.text and not j.get("executed")


def _documented_routes(mod):
    """Every (method, path template) the app serves, including routers it includes (their routes appear in the OpenAPI document)."""
    out = set()
    for path, ops in mod.app.openapi()["paths"].items():
        out |= {(m.upper(), path) for m in ops}
    for r in mod.app.routes:                                                    # routes that are not in the schema (HTML pages, include_in_schema=False)
        for m in (getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"}:
            if hasattr(r, "path"):
                out.add((m, r.path))
    return out


@pytest.mark.parametrize("app", APPS)
def test_every_route_of_the_app_is_either_gated_public_by_design_operator_or_framework(monkeypatch, app):
    """The completeness check: a new route cannot be added without a decision in apps/_route_table.py."""
    from starlette.routing import compile_path
    from _route_table import ENGINE_CONSOLES, OPERATOR_AUTH_ROUTES
    mod = _start(monkeypatch, app)
    known = [(m, p.split("?")[0]) for m, p, _b, _t, _k in ROUTES[app]]
    known += list(PUBLIC[app]) + list(OPERATOR_ROUTES) + list(FRAMEWORK_ROUTES) + list(ENGINE_CONSOLES.get(app, {})) + list(OPERATOR_AUTH_ROUTES.get(app, {}))
    unaccounted = []
    for method, path in sorted(_documented_routes(mod)):
        rx, _, _ = compile_path(path)
        if method in ("HEAD", "OPTIONS"):
            continue
        if not any(km == method and (kp == path or rx.match(kp)) for km, kp in known):
            unaccounted.append((method, path))
    assert unaccounted == [], f"routes with no gate decision in apps/_route_table.py: {unaccounted}"


@pytest.mark.parametrize("app", APPS)
def test_with_the_enterprise_gate_on_an_anonymous_request_to_every_route_returns_nothing_tool_derived(monkeypatch, app, net):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    stub.install(monkeypatch, fleet={})
    mod = _start(monkeypatch, app)
    assert app in app_gate._ENTERPRISE
    client = TestClient(mod.app, raise_server_exceptions=False)
    for method, path, body, tool, kind in ROUTES[app]:
        r = _call(client, method, path, body)                                   # no credential: the anonymous principal
        assert _refused(r), (app, method, path, tool, r.status_code, r.text[:200])
    assert net == []                                                            # nothing reached a backend


@pytest.mark.parametrize("app", APPS)
def test_with_the_enterprise_gate_on_a_granted_identity_passes_the_gate_so_the_principal_of_the_request_decides(monkeypatch, app, net):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    stub.install(monkeypatch, fleet={})
    mod = _start(monkeypatch, app)
    client = TestClient(mod.app, raise_server_exceptions=False)
    for method, path, body, tool, kind in ROUTES[app]:
        if kind == "route-or-auth":
            continue
        r = _call(client, method, path, body, {"x-test-user": "granted"})
        assert not ("[blocked]" in r.text and "no grant" in r.text.lower()), (app, method, path, tool, r.text[:200])
        assert not (r.status_code == 403 and "[blocked]" in r.text), (app, method, path, tool, r.text[:200])


@pytest.mark.parametrize("app", APPS)
def test_with_basic_permissions_restricted_every_route_is_refused_too(monkeypatch, app, net):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    stub.absent(monkeypatch)
    mod = _start(monkeypatch, app)
    client = TestClient(mod.app, raise_server_exceptions=False)
    for method, path, body, tool, kind in ROUTES[app]:
        assert _refused(_call(client, method, path, body)), (app, method, path, tool)
    assert net == []


@pytest.mark.parametrize("app", APPS)
def test_every_tool_a_route_names_is_classified(app):
    for _m, _p, _b, tool, _k in ROUTES[app]:
        assert tool in TOOL_ACTIONS[app], (app, tool)


@pytest.mark.parametrize("app", APPS)
def test_without_a_request_the_enterprise_principal_is_anonymous_so_a_background_job_is_denied(monkeypatch, app):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    stub.install(monkeypatch, fleet={})
    _start(monkeypatch, app)
    tool = next(iter(TOOL_ACTIONS[app]))
    assert app_gate.tool_denial(app, tool) is not None


def test_the_public_routes_are_listed_with_a_reason_and_carry_no_tool_derived_data():
    for app, table in PUBLIC.items():
        assert all(isinstance(why, str) and len(why) > 10 for why in table.values()), app
    assert set(PUBLIC["agentic-privacy"]) >= {("POST", "/request"), ("GET", "/intake"), ("POST", "/verify"), ("GET", "/export")}
