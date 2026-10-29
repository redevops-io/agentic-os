"""DR-01 (design decision): the privacy portal's public submission is a TIGHTLY CONSTRAINED exception, not a bypass.

``public_intake`` is a distinct trusted classification, separate from read / write grants. It exists only for routes registered by id in trusted code (``tool_actions.PUBLIC_INTAKE_ROUTES``: app, method, path ->
the one tool it may create a PENDING request for). A ticket for it is minted by ``app_gate.public_intake`` and is accepted only by the one core function that creates a pending intake record. The underlying
``privacy.intake`` capability stays classified ``write`` for every ordinary interface (Operator, ``/invoke``, console), which a read-only principal and an anonymous caller still cannot use.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate, tool_actions  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402

APP = "agentic-privacy"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "1")
    app_gate.reset_state()
    stub.absent(monkeypatch)
    yield
    app_gate.reset_state()


def start(monkeypatch, tmp_path):
    for n in [n for n in sys.modules if n in (f"{APP}.app", f"{APP}.core", f"{APP}.operator")]:
        monkeypatch.delitem(sys.modules, n)
    core = importlib.import_module(f"{APP}.core")
    monkeypatch.setattr(core, "DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)
    return core, importlib.import_module(f"{APP}.app")


# ── the registry ──

def test_the_registry_is_exactly_the_two_portal_routes():
    assert tool_actions.PUBLIC_INTAKE_ROUTES == {APP: {("POST", "/intake"): "privacy.intake", ("POST", "/request"): "privacy.intake"}}


def test_the_tool_behind_the_exception_stays_classified_write():
    assert tool_actions.TOOL_ACTIONS[APP]["privacy.intake"] == "write"
    for app, routes in tool_actions.PUBLIC_INTAKE_ROUTES.items():
        for tool in routes.values():
            assert tool_actions.TOOL_ACTIONS[app][tool] == "write"


def test_a_ticket_is_minted_only_for_a_registered_route_id():
    t = app_gate.public_intake(APP, "POST", "/intake")
    assert app_gate.is_public_intake(t, APP, "privacy.intake")
    for bad in (("agentic-privacy", "POST", "/fulfil/r1"), ("agentic-privacy", "GET", "/intake"), ("agentic-privacy", "POST", "/intake/"), ("agentic-privacy", "POST", "/INTAKE"),
                ("agentic-privacy", "post", "/intake "), ("agentic-privacy", "POST", "/verify"), ("books", "POST", "/intake"), ("privacy", "POST", "/intake"), ("", "", ""),
                ("agentic-privacy", "POST", "/retention"), ("agentic-privacy", "POST", "/invoke"), ("agentic-privacy", "POST", "/api/agent")):
        with pytest.raises(app_gate.GateStartupError):
            app_gate.public_intake(*bad)


def test_a_ticket_cannot_be_forged_copied_or_reused_for_another_tool():
    real = app_gate.public_intake(APP, "POST", "/intake")
    assert not app_gate.is_public_intake(real, APP, "privacy.delete") and not app_gate.is_public_intake(real, "books", "privacy.intake")
    forged = type(real)(APP, "POST", "/intake", "privacy.intake", object())               # right shape, no seal
    assert not app_gate.is_public_intake(forged, APP, "privacy.intake")
    for junk in (None, "POST /intake", {"app": APP}, ("agentic-privacy", "POST", "/intake"), object()):
        assert not app_gate.is_public_intake(junk, APP, "privacy.intake")


def test_the_core_function_that_creates_the_pending_request_refuses_anything_but_a_ticket(monkeypatch, tmp_path):
    core, _app = start(monkeypatch, tmp_path)
    for junk in (None, "yes", True, {"app": APP}):
        with pytest.raises(PermissionError):
            core.public_intake(junk, "jane@example.com", "access", "http://testserver/")
    assert core._load_requests() == []


def test_the_registry_is_trusted_code_not_configuration(monkeypatch):
    monkeypatch.setenv("PRIVACY_PUBLIC_INTAKE_ROUTES", "POST /fulfil/{id}")
    monkeypatch.setenv("AGENTIC_OS_PUBLIC_INTAKE", "books:POST:/invoke")
    with pytest.raises(app_gate.GateStartupError):
        app_gate.public_intake("books", "POST", "/invoke")
    with pytest.raises(app_gate.GateStartupError):
        app_gate.public_intake(APP, "POST", "/fulfil/{id}")


def test_only_the_privacy_app_and_the_walk_registers_the_routes():
    """Every handler that mints a ticket is one of the two registered routes (a new use is a deliberate change here)."""
    import ast
    root = pathlib.Path(__file__).resolve().parents[1]
    users = []
    for f in sorted([*(root / "apps").glob("*/*.py"), *(root / "agentic_os").rglob("*.py")]):
        if f.name.startswith("test_"):
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and ((isinstance(n.func, ast.Attribute) and n.func.attr == "public_intake" and isinstance(n.func.value, ast.Name) and n.func.value.id == "app_gate") or (isinstance(n.func, ast.Name) and n.func.id in ("public_intake", "_public_intake_ticket"))):
                if f.name != "app_gate.py":
                    users.append(str(f.relative_to(root)))
    assert users and set(users) == {"apps/agentic-privacy/app.py"} and len(users) == 2


# ── the underlying capability stays write for the ordinary interfaces ──

@pytest.mark.parametrize("mode", ["unprovisioned", "read-only", "foreign"])
def test_privacy_intake_is_still_denied_through_every_ordinary_interface(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    if mode == "read-only":
        init_local_profile(str(tmp_path / "perm"))
    elif mode == "foreign":
        import json
        init_local_profile(str(tmp_path / "perm"), user="alice", include_write=True)
        (tmp_path / "perm" / "profile.json").write_text(json.dumps({"version": 1, "user": "mallory"}))
    core, appmod = start(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(core, "_send_email", lambda *a: sent.append(a) or "postmark")
    monkeypatch.setattr(core, "PRIVACY_PUBLIC_URL", "https://privacy.example.org")
    client = TestClient(appmod.app, raise_server_exceptions=False)
    # /invoke (the operator's HTTP surface)
    r = client.post("/invoke", json={"capability": "privacy.intake", "inputs": {"email": "jane@example.com", "type": "access"}})
    assert r.status_code == 200 and r.json()["result"]["status"] == "denied" and "[blocked]" in r.json()["result"]["error"]
    # direct Operator
    op = importlib.import_module(f"{APP}.operator")
    operator = getattr(op, next(n for n in dir(op) if n.startswith("build_") and n.endswith("operator")))()
    out = operator.invoke("privacy.intake", {"email": "jane@example.com", "type": "access"})
    assert out["status"] == "denied" and out["executed"] is False
    # the console
    r = client.post("/api/agent", json={"message": "file an intake request for jane@example.com"})
    assert r.status_code in (200, 403, 503) and "received" not in r.text
    assert sent == [] and core._load_requests() == []                                         # nothing was created or e-mailed
    # ... while the public route, with no grant at all, creates exactly its PENDING request
    r = client.post("/intake", json={"email": "jane@example.com", "type": "access"})
    assert r.status_code == 200 and r.json()["status"] == "received" and len(sent) == 1 and len(core._load_requests()) == 1


def test_privacy_intake_is_denied_to_an_anonymous_enterprise_caller_too(monkeypatch, tmp_path):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    st = stub.install(monkeypatch, fleet={})
    core, appmod = start(monkeypatch, tmp_path)
    operator = getattr(importlib.import_module(f"{APP}.operator"), "build_privacy_operator", None)
    sent = []
    monkeypatch.setattr(core, "_send_email", lambda *a: sent.append(a) or "postmark")
    client = TestClient(appmod.app, raise_server_exceptions=False)
    r = client.post("/invoke", json={"capability": "privacy.intake", "inputs": {"email": "jane@example.com"}})
    assert r.status_code == 200 and r.json()["result"]["status"] == "denied" and sent == []
    del st, operator


def test_the_exception_is_not_a_general_operator_bypass(monkeypatch, tmp_path):
    """A ticket is not an argument of the Operator: no input of /invoke or Operator.invoke can carry one."""
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    core, appmod = start(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(core, "_send_email", lambda *a: sent.append(a) or "postmark")
    client = TestClient(appmod.app, raise_server_exceptions=False)
    for extra in ({"ticket": True}, {"public_intake": True}, {"_public_intake": "ticket"}, {"route": "POST /intake"}):
        r = client.post("/invoke", json={"capability": "privacy.intake", "inputs": {"email": "jane@example.com", **extra}})
        assert r.json()["result"]["status"] == "denied"
    assert sent == []


def test_the_exception_is_documented_in_the_guide_the_changelog_and_the_inventory():
    root = pathlib.Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "PERMISSIONS_MIGRATION.md").read_text().split("## 30.", 1)[1]
    for needle in ("public by design", "PUBLIC_INTAKE_ROUTES", "api.postmarkapp.com/email", "PRIVACY_PUBLIC_URL", "PRIVACY_INTAKE_GLOBAL_PER_HOUR", "PRIVACY_PENDING_MAX", "honeypot", "MASKED",
                   "stays classified `write`", "public intake: unauthenticated by design"):
        assert needle in guide, needle
    assert "PUBLIC_INTAKE_ROUTES" in (root / "CHANGELOG.md").read_text()
    assert "public intake: unauthenticated by design" in (root / "docs" / "TOOL_CLASSIFICATION_INVENTORY.md").read_text()
