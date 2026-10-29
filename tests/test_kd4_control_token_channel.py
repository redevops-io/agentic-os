"""KD-4: the operator's CALLER credential (the control / tenant token) and the END USER's identity token travel on separate channels.

``Authorization: Bearer`` is where an enterprise identity layer reads the user's JWT. Until KD-4 the operator's ``POST /invoke`` read the control token from the same header, so with a
control token configured (every production deployment) a token-identified user could not use ``/invoke``: the JWT was refused as a control credential, or the control token made the
request anonymous. The control credential now has its own header, ``X-Control-Token``; ``Authorization: Bearer <control token>`` keeps working for existing callers (it is just no longer
the only way). Neither credential can stand in for the other.

The user identity here is a stand-in for the enterprise gate: ``principal_for`` reads ``Authorization: Bearer user:<name>:<signature>``; the authorizer allows the user ``ana`` only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os import app_gate
from agentic_os.mission.operator_sdk import Operator, capability

CTL = "control-token-" + "c" * 24
TENANT = "tenant-token-" + "t" * 24
GOOD_SIG = "valid"


def principal_for(request):
    """The stand-in for the verified identity: only a well-formed 'user:<name>:valid' bearer identifies; anything else (forged, absent, the control token) is anonymous."""
    value = (getattr(request, "headers", {}) or {}).get("authorization", "") if request is not None else ""
    scheme, _, token = value.partition(" ")
    parts = token.split(":")
    if scheme.lower() == "bearer" and len(parts) == 3 and parts[0] == "user" and parts[2] == GOOD_SIG:
        return SimpleNamespace(user=parts[1], roles=[])
    return SimpleNamespace(user="", roles=[])


def authorizer(principal, spec, ctx):
    return None if principal.user == "ana" else f"{principal.user or 'anonymous'} holds no grant"


@pytest.fixture
def world(monkeypatch):
    for k in ("AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", CTL)
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {"kd4": (principal_for, {"kd4.look": "read"})})
    monkeypatch.setattr(app_gate, "_cr_default_authorizer", lambda: authorizer)
    ran = []
    op = Operator("kd4", [capability("kd4.look", lambda i: (ran.append(i), {"ok": True})[1])])
    app = FastAPI()
    app.include_router(op.router())
    app.add_middleware(app_gate.RequestContext)
    return ran, TestClient(app, raise_server_exceptions=False)


def post(client, headers):
    return client.post("/invoke", json={"capability": "kd4.look", "inputs": {}}, headers=headers)


def user(name="ana", sig=GOOD_SIG):
    return f"Bearer user:{name}:{sig}"


def test_a_request_carrying_both_credentials_is_authenticated_for_each_purpose_independently(world):
    ran, c = world
    r = post(c, {"X-Control-Token": CTL, "Authorization": user("ana")})
    assert r.status_code == 200 and r.json()["result"] == {"ok": True} and len(ran) == 1         # control token: may call /invoke; JWT: ana, who holds the grant
    r = post(c, {"X-Control-Token": CTL, "Authorization": user("bob")})
    assert r.status_code == 200 and r.json()["result"]["status"] == "denied" and "bob" in r.json()["result"]["error"] and len(ran) == 1   # identified as bob: no grant


def test_the_control_token_alone_authenticates_the_caller_but_not_the_user(world):
    ran, c = world
    r = post(c, {"X-Control-Token": CTL})
    assert r.status_code == 200 and r.json()["result"]["status"] == "denied" and "anonymous" in r.json()["result"]["error"] and ran == []


def test_a_forged_or_invalid_user_token_is_anonymous_even_with_a_valid_control_token(world):
    ran, c = world
    for bad in (user("ana", "forged"), "Bearer user:ana", "Bearer garbage", "Basic " + CTL, user("ana") + "x"):
        r = post(c, {"X-Control-Token": CTL, "Authorization": bad})
        assert r.status_code == 200 and r.json()["result"]["status"] == "denied", bad
    assert ran == []


def test_neither_credential_can_be_used_in_place_of_the_other(world):
    ran, c = world
    assert post(c, {"Authorization": user("ana")}).status_code == 401                              # the user's token is not a control credential
    assert post(c, {"X-Control-Token": user("ana")}).status_code == 401                            # nor in the control header
    assert post(c, {"X-Control-Token": "Bearer " + CTL}).status_code == 401                        # the header takes the bare token
    r = post(c, {"X-Control-Token": CTL, "Authorization": "Bearer " + CTL})                        # the control token in the identity channel identifies nobody
    assert r.status_code == 200 and r.json()["result"]["status"] == "denied"
    assert post(c, {"X-Control-Token": "wrong", "Authorization": "Bearer " + CTL}).status_code == 401      # a wrong control header is not rescued by a correct Bearer
    assert post(c, {"X-Control-Token": "wrong", "Authorization": user("ana")}).status_code == 401
    assert post(c, {}).status_code == 401
    assert ran == []


def test_the_legacy_bearer_control_token_still_works_for_existing_callers(world, monkeypatch):
    ran, c = world
    monkeypatch.setattr(app_gate, "_ENTERPRISE", {})                                              # no enterprise identity: the original contract
    r = post(c, {"Authorization": "Bearer " + CTL})
    assert r.status_code == 200 and r.json()["result"] == {"ok": True} and len(ran) == 1
    r = post(c, {"X-Control-Token": CTL})
    assert r.status_code == 200 and len(ran) == 2
    r = post(c, {"X-Control-Token": " ", "Authorization": "Bearer " + CTL})                         # a blank header is no credential: the Bearer decides
    assert r.status_code == 200 and len(ran) == 3
    assert post(c, {"Authorization": "Bearer nope"}).status_code == 401


def test_tenant_tokens_use_the_same_channel(world, monkeypatch):
    ran, c = world
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TENANT}")
    r = post(c, {"X-Control-Token": TENANT, "Authorization": user("ana")})
    assert r.status_code == 200 and r.json()["result"] == {"ok": True}
    r = c.post("/invoke", json={"capability": "kd4.look", "inputs": {"_tenant": "globex"}}, headers={"X-Control-Token": TENANT, "Authorization": user("ana")})
    assert r.status_code == 403                                                                    # the tenant credential still pins the tenant
    assert post(c, {"X-Control-Token": TENANT + "x", "Authorization": user("ana")}).status_code == 401


def test_without_any_token_configured_the_header_changes_nothing(world, monkeypatch):
    ran, c = world
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN")
    assert post(c, {"X-Control-Token": CTL, "Authorization": user("ana")}).status_code == 503      # fails closed, as before
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    r = post(c, {"Authorization": user("ana")})
    assert r.status_code == 200 and r.json()["result"] == {"ok": True}                              # dev flag: the JWT identifies, no control token needed
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", CTL)
    assert post(c, {"Authorization": user("ana")}).status_code == 401                              # once a token is configured the flag is ignored


# ── BB-02: a duplicated credential header is a smuggling signal ──

def post_headers(client, headers):
    return client.post("/invoke", json={"capability": "kd4.look", "inputs": {}}, headers=headers)


def test_more_than_one_control_token_header_is_refused_whatever_the_order(world):
    ran, c = world
    for pair in ((CTL, CTL), (CTL, "wrong"), ("wrong", CTL), ("wrong", "also-wrong"), (CTL, "")):
        r = post_headers(c, [("X-Control-Token", pair[0]), ("X-Control-Token", pair[1]), ("Authorization", user("ana"))])
        assert r.status_code == 401, pair
    assert ran == []
    r = post_headers(c, [("X-Control-Token", CTL), ("x-control-token", CTL)])               # differently cased spellings of the same name are still two headers
    assert r.status_code == 401


def test_more_than_one_authorization_header_is_refused_too(world):
    ran, c = world
    for pair in ((user("ana"), user("ana")), (user("ana"), "Bearer " + CTL), ("Bearer " + CTL, user("ana"))):
        assert post_headers(c, [("X-Control-Token", CTL), ("Authorization", pair[0]), ("Authorization", pair[1])]).status_code == 401
        assert post_headers(c, [("Authorization", pair[0]), ("Authorization", pair[1])]).status_code == 401
    assert ran == []


def test_a_control_token_and_a_different_valid_credential_in_authorization_are_a_conflict(world, monkeypatch):
    ran, c = world
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TENANT}")
    assert post_headers(c, {"X-Control-Token": CTL, "Authorization": "Bearer " + TENANT}).status_code == 401       # two different callers claimed at once
    assert post_headers(c, {"X-Control-Token": TENANT, "Authorization": "Bearer " + CTL}).status_code == 401
    assert post_headers(c, {"X-Control-Token": CTL, "Authorization": "Bearer " + CTL}).status_code == 200         # the same credential twice is not a conflict
    assert post_headers(c, {"X-Control-Token": CTL, "Authorization": user("ana")}).json()["result"] == {"ok": True}   # the intended pair: a control token and a user's JWT
    assert len(ran) == 1


def test_a_single_header_of_each_kind_still_works(world):
    ran, c = world
    assert post_headers(c, {"X-Control-Token": CTL, "Authorization": user("ana")}).status_code == 200
    assert post_headers(c, {"X-Control-Token": CTL}).status_code == 200
    assert post_headers(c, {"Authorization": "Bearer " + CTL}).status_code == 200
