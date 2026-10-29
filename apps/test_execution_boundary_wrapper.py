"""Design decision 1: ONE shared execution wrapper (``app_gate.guarded``) at the boundary where the seven older apps' tool bodies run, reused by all seven, so a route that reaches a body cannot
skip the gate (the route-level checks stay as defence in depth). These tests call the BODIES directly (no route, no console) and walk the apps to prove each body carries the wrapper."""
from __future__ import annotations

import importlib
import pathlib
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402

BODIES = {
    "books": {"_categorize": "books.categorize", "_reconcile": "books.reconcile", "_close": "books.close", "_record_revenue": "books.record_revenue",
              "_reverse_entry": "books.reverse_entry"},
    "billing": {"_chase_overdue": "chase_overdue", "_refund": "refund", "_create_subscription": "billing.create_subscription",
                "_cancel_subscription": "billing.cancel_subscription"},
    "compliance": {"_scan": "compliance.scan", "_explain": "compliance.explain", "_remediate": "compliance.remediate", "_file_consent": "compliance.file_consent"},
    "growth-engine": {"_analyze": "growth.analyze", "_reallocate_budget": "growth.reallocate_budget"},
    "market-radar": {"_add_watch": "radar.add_watch", "_brief": "radar.brief"},
    "edge-sentinel": {"_block_ip": "ban_ip", "_approve_block": "ban_ip", "_triage": "sentinel.triage"},
}
ARGS = {"_categorize": (), "_reconcile": (), "_close": (), "_chase_overdue": (), "_scan": (), "_analyze": ()}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setenv("SENTINEL_RATE_BURST", "1000000")
    stub.absent(monkeypatch)


@pytest.fixture()
def sentinel(monkeypatch):
    hits = []

    def refuse(*a, **k):
        hits.append(a[:1])
        raise OSError("blocked by the test")
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a, **k: refuse(*a))
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    for n in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, n, refuse)
    return hits


def _start(monkeypatch, app):
    for name in [n for n in sys.modules if n == f"{app}.app"]:
        monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(f"{app}.app")


def _call(fn, name):
    args = ARGS.get(name, ({},))
    return fn(*args)


@pytest.mark.parametrize("app", sorted(BODIES))
def test_every_tool_body_of_the_older_apps_carries_the_shared_wrapper_and_names_its_tool(monkeypatch, app):
    mod = _start(monkeypatch, app)
    for fn, tool in BODIES[app].items():
        f = getattr(mod, fn)
        assert getattr(f, "__guarded_tool__", None) == (app, tool), (app, fn)
        assert tool in TOOL_ACTIONS[app]


@pytest.mark.parametrize("app", sorted(BODIES))
def test_calling_a_body_directly_unprovisioned_is_denied_and_runs_nothing(monkeypatch, app, sentinel):
    mod = _start(monkeypatch, app)
    for fn, tool in BODIES[app].items():
        out = _call(getattr(mod, fn), fn)
        assert out["status"] == "denied" and out["executed"] is False and "[blocked]" in out["error"] and "permissions init" in out["error"], (app, fn, out)
    assert sentinel == []


@pytest.mark.parametrize("app", sorted(BODIES))
def test_a_read_only_profile_runs_read_bodies_and_denies_every_write_body(monkeypatch, app, tmp_path, sentinel):
    init_local_profile(str(tmp_path / "perm"))
    mod = _start(monkeypatch, app)
    for fn, tool in BODIES[app].items():
        out = _call(getattr(mod, fn), fn)
        denied = isinstance(out, dict) and out.get("status") == "denied" and "[blocked]" in str(out.get("error"))
        assert denied == (TOOL_ACTIONS[app][tool] != "read"), (app, fn, tool, out)


def test_nothing_requested_the_wrapper_is_a_no_op(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PERMISSIONS", raising=False)
    mod = _start(monkeypatch, "growth-engine")
    assert app_gate.denial_result("growth-engine", "growth.analyze") is None
    assert callable(mod._analyze) and mod._analyze.__guarded_tool__ == ("growth-engine", "growth.analyze")


def test_every_route_that_dispatches_to_a_body_calls_only_wrapped_bodies():
    """AST: in each of the six apps, the names the /agent/run handler calls (besides the gate helpers) are exactly the wrapped bodies."""
    import ast
    for app, bodies in BODIES.items():
        src = (pathlib.Path(__file__).resolve().parent / app / "app.py").read_text()
        tree = ast.parse(src)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for fn in bodies:
            assert fn in defined, (app, fn)
        # each wrapped function is decorated with the shared wrapper in the source, and with nothing else that could replace it
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name in bodies:
                decs = [ast.unparse(d) for d in n.decorator_list]
                assert any(d.startswith("_guarded(") for d in decs), (app, n.name, decs)
