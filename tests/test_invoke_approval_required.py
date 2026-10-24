"""F21: `approval_required` is enforced on every /invoke path, in every profile.

A capability that declares a mandatory approval does not run for a direct call without a verified human approval, whether the
process is in development or production. Development may use an explicit testing override; it never silently reinterprets
the requirement, and production ignores the override."""
from __future__ import annotations

import secrets as _secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.approval import mint_approval_token
from agentic_os.mission.execution_auth import mint_execution_authorization
from agentic_os.mission.operator_sdk import Operator, capability
from agentic_os.mission.verify import is_staged

SECRET = _secrets.token_hex(24)
TOKEN = "control-token"


class Ran:
    def __init__(self):
        self.calls = []


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Default posture: no testing override, signing secret and control token configured."""
    for k in ("AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    ran = Ran()
    op = Operator("fin", [
        capability("fin.refund", lambda i: (ran.calls.append(("refund", i)), {"refunded": True})[1],
                   side_effecting=True, approval_required=True),
        capability("fin.report", lambda i: (ran.calls.append(("report", i)), {"ok": True})[1], approval_required=True),
        capability("fin.lookup", lambda i: (ran.calls.append(("lookup", i)), {"found": True})[1]),
    ])
    app = FastAPI()
    app.include_router(op.router())
    return ran, TestClient(app, raise_server_exceptions=False)


def call(c, cap, inputs=None, **extra):
    body = {"capability": cap, "inputs": inputs or {}, "idempotency_key": "k1", **extra}
    return c.post("/invoke", json=body, headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "k1"})


@pytest.mark.parametrize("profile", ["development", "test"])
def test_a_direct_call_to_an_approval_required_capability_stages_in_every_non_production_profile(world, monkeypatch, profile):
    ran, c = world
    monkeypatch.setenv("AGENTIC_OS_PROFILE", profile)
    for cap in ("fin.refund", "fin.report"):                    # side-effecting or not: the declaration decides
        r = call(c, cap, {"amount": 5})
        assert r.status_code == 200 and is_staged(r.json()["result"]), cap
        assert r.json()["result"]["executed"] is False and r.json()["result"]["reason"] == "approval_required"
    assert ran.calls == []                                        # the handler never ran


def test_an_unset_profile_is_not_a_loophole(world):
    ran, c = world
    assert is_staged(call(c, "fin.refund", {"amount": 5}).json()["result"]) and ran.calls == []


def test_production_refuses_before_the_handler(world, monkeypatch):
    ran, c = world
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    r = call(c, "fin.refund", {"amount": 5})
    assert r.status_code == 403 and ran.calls == []
    assert is_staged(call(c, "fin.report").json()["result"])      # not side-effecting: stages instead of refusing


def test_forged_approval_flag_in_the_body_changes_nothing(world):
    ran, c = world
    r = call(c, "fin.refund", {"amount": 5, "_approval": {"approved": True}, "approval": True, "_Approval": 1})
    assert is_staged(r.json()["result"]) and ran.calls == []


def test_a_verified_human_approval_runs_it_once(world):
    ran, c = world
    tok = mint_approval_token(SECRET, operator="fin", capability="fin.refund", mission_id="m1", node_id="n1",
                              inputs={"amount": 5}, marker={"approved": True})
    r = call(c, "fin.refund", {"amount": 5}, approval_token=tok, mission_id="m1", node_id="n1")
    assert r.status_code == 200 and r.json()["result"] == {"refunded": True} and len(ran.calls) == 1
    again = call(c, "fin.refund", {"amount": 5}, approval_token=tok, mission_id="m1", node_id="n1")
    assert is_staged(again.json()["result"]) and len(ran.calls) == 1        # single-use


def test_an_approval_for_other_inputs_does_not_apply(world):
    ran, c = world
    tok = mint_approval_token(SECRET, operator="fin", capability="fin.refund", mission_id="m1", node_id="n1",
                              inputs={"amount": 5}, marker={"approved": True})
    r = call(c, "fin.refund", {"amount": 5000}, approval_token=tok, mission_id="m1", node_id="n1")
    assert is_staged(r.json()["result"]) and ran.calls == []


def test_an_execution_authorization_cannot_stand_in_for_a_mandatory_approval(world):
    ran, c = world
    tok = mint_execution_authorization(SECRET, operator="fin", capability="fin.refund", tenant="acme", mission_id="m1",
                                       node_id="n1", lease_id="m1:n1:a1", attempt=1, envelope_digest="d",
                                       inputs={"amount": 5}, idempotency_key="k1")
    assert call(c, "fin.refund", {"amount": 5}, authorization=tok).status_code == 403 and ran.calls == []


def test_the_explicit_testing_override_works_in_development_only(world, monkeypatch):
    ran, c = world
    monkeypatch.setenv("AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "1")
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    assert call(c, "fin.refund", {"amount": 5}).json()["result"] == {"refunded": True} and len(ran.calls) == 1
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    r = call(c, "fin.refund", {"amount": 6})
    assert r.status_code == 403 and len(ran.calls) == 1                     # production ignores the override


def test_capabilities_that_do_not_declare_an_approval_are_unaffected(world):
    ran, c = world
    assert call(c, "fin.lookup").json()["result"] == {"found": True} and len(ran.calls) == 1
