"""Authorise, THEN approve, THEN execute (the order on an operator's ``POST /invoke``).

``/invoke`` used to answer a capability that needs an approval with ``pending_approval`` before any grant was consulted, for every caller including an anonymous one. That leaks the existence and the
side-effect status of a capability to a caller who may not execute it, and lets such a caller queue approval requests (spam for the approvers). The grant is now asked first: a caller who may not run
the capability gets the denial, not an approval prompt, a 403 about execution authorization, or a 404; and nothing single-use (an approval token or an execution authorization nonce) is consumed by a
denied call.
"""
from __future__ import annotations

import secrets as _secrets
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os import app_gate
from agentic_os.mission.approval import mint_approval_token
from agentic_os.mission.operator_sdk import Operator, capability
from agentic_os.mission.verify import is_staged

SECRET = _secrets.token_hex(24)
CTL = "control-token-" + "c" * 24


def principal_for(request):
    h = request.headers if request is not None else {}
    return SimpleNamespace(app="aao", user=h.get("x-user", ""), roles=[])


def check(principal, spec, ctx):
    return None if principal.user == "ana" else f"{principal.user or 'anonymous'} holds no grant"


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS", "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "AGENTIC_OS_PROFILE", "AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", CTL)
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {"aao": (principal_for, {"aao.refund": "write", "aao.lookup": "read"})})
    monkeypatch.setattr(app_gate, "_cr_default_authorizer", lambda: check)
    ran = []
    op = Operator("aao", [capability("aao.refund", lambda i: (ran.append(i), {"refunded": True})[1], side_effecting=True, approval_required=True),
                          capability("aao.lookup", lambda i: (ran.append(i), {"found": True})[1])])
    app = FastAPI()
    app.include_router(op.router())
    app.add_middleware(app_gate.RequestContext)
    return op, ran, TestClient(app, raise_server_exceptions=False)


def post(c, cap="aao.refund", user="", inputs=None, **extra):
    h = {"X-Control-Token": CTL}
    if user:
        h["x-user"] = user
    return c.post("/invoke", json={"capability": cap, "inputs": inputs or {"amount": 5}, **extra}, headers=h)


def test_an_anonymous_caller_gets_a_denial_not_an_approval_prompt(world):
    op, ran, c = world
    r = post(c)
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["status"] == "denied" and res["executed"] is False and "[blocked]" in res["error"] and not is_staged(res)
    assert "approval" not in str(res).lower() and ran == []


def test_a_caller_without_the_grant_learns_nothing_about_which_capabilities_need_approval(world):
    op, ran, c = world
    gated = post(c, "aao.refund", user="bob").json()["result"]
    plain = post(c, "aao.lookup", user="bob").json()["result"]
    assert gated["status"] == plain["status"] == "denied"
    assert {k for k in gated} == {k for k in plain}                                           # the same shape: no staging marker, no reason that names the approval
    assert "pending" not in str(gated)


def test_an_unknown_capability_is_denied_like_a_known_one_for_a_caller_without_grants(world):
    op, ran, c = world
    r = post(c, "aao.does_not_exist", user="bob")
    assert r.status_code == 200 and r.json()["result"]["status"] == "denied"                  # not a 404 that tells the two apart


def test_a_caller_who_may_run_it_is_asked_for_the_approval_as_before(world):
    op, ran, c = world
    res = post(c, user="ana").json()["result"]
    assert is_staged(res) and res["reason"] == "approval_required" and ran == []


def test_a_denied_call_does_not_consume_a_single_use_approval_token(world):
    op, ran, c = world
    inputs = {"amount": 5}
    token = mint_approval_token(SECRET, operator="aao", capability="aao.refund", inputs=inputs, mission_id="m", node_id="n", marker={"approved": True, "by": "alice"})
    denied = post(c, inputs=inputs, approval_token=token, mission_id="m", node_id="n").json()["result"]       # anonymous: denied, the token was not verified
    assert denied["status"] == "denied" and ran == []
    ok = post(c, user="ana", inputs=inputs, approval_token=token, mission_id="m", node_id="n").json()["result"]
    assert ok == {"refunded": True} and len(ran) == 1                                                       # the same token still works for the caller who is allowed


def test_in_production_a_caller_without_the_grant_gets_the_denial_not_the_execution_authorization_error(world, monkeypatch):
    op, ran, c = world
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    r = post(c)
    assert r.status_code == 200 and r.json()["result"]["status"] == "denied"
    allowed = post(c, user="ana")
    assert allowed.status_code == 403 and "execution authorization" in allowed.text                         # the allowed caller still meets the profile's rule


def test_basic_mode_unprovisioned_and_read_only_profiles_are_denied_before_approval(monkeypatch, tmp_path):
    from agentic_os.permissions_setup import init_local_profile
    for k in ("CR_PERMISSIONS", "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "AGENTIC_OS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", CTL)
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {})

    def client():
        app_gate.install_gate("growth-assistant")
        op = Operator("growth-assistant", [capability("assistant.push_assets", lambda i: {"pushed": True}, side_effecting=True, approval_required=True)])
        app = FastAPI()
        app.include_router(op.router())
        return TestClient(app, raise_server_exceptions=False)
    body = {"capability": "assistant.push_assets", "inputs": {"asset_id": "x"}}
    hdr = {"X-Control-Token": CTL}
    assert client().post("/invoke", json=body, headers=hdr).json()["result"]["status"] == "denied"          # restricted start
    init_local_profile(str(tmp_path / "perm"))                                                              # read grants only
    assert client().post("/invoke", json=body, headers=hdr).json()["result"]["status"] == "denied"
    init_local_profile(str(tmp_path / "perm"), include_write=True)
    assert is_staged(client().post("/invoke", json=body, headers=hdr).json()["result"])                     # write grant: now it asks for the approval


def test_without_permissions_requested_nothing_changes(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS", "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "AGENTIC_OS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", CTL)
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {})
    op = Operator("plain", [capability("plain.refund", lambda i: {"ok": True}, side_effecting=True, approval_required=True)])
    app = FastAPI()
    app.include_router(op.router())
    c = TestClient(app, raise_server_exceptions=False)
    assert is_staged(c.post("/invoke", json={"capability": "plain.refund", "inputs": {}}, headers={"X-Control-Token": CTL}).json()["result"])
    assert c.post("/invoke", json={"capability": "plain.nope", "inputs": {}}, headers={"X-Control-Token": CTL}).status_code == 404
