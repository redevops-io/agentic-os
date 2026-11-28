"""An approval says whose work it approves and who approved it: bound into the signed token, enforced at the operator."""
import secrets as _secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.approval import (
    ReplayGuard, approver_identity_required, mint_approval_token, verify_approval_token)
from agentic_os.mission.authority_policy import AuthorityTestPolicy
from agentic_os.mission.operator_sdk import Operator, capability

SECRET = _secrets.token_hex(24)
ADMIN, ACME, GLOBEX = ("control-" + _secrets.token_hex(12), "acme-" + _secrets.token_hex(16), "globex-" + _secrets.token_hex(16))
INPUTS = {"account": "A-1", "iban": "DE00"}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_TENANT_LIMITS", "AGENTIC_OS_REQUIRE_APPROVER_IDENTITY", "AGENTIC_OS_TENANT_TOKENS",
              "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_AUTHORITY_DB",
              "AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE"):                      # the suite-wide override would run a refused call anyway
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={ACME},globex={GLOBEX}")
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    return monkeypatch


def mint(inputs=INPUTS, marker=None, **kw):
    return mint_approval_token(operator="crm", capability="crm.bank_change", mission_id="m-1", node_id="n-1", inputs=inputs,
                               marker=marker if marker is not None else {"approved": True, "by": "approver:acme", "tenant": "acme"}, **kw)


def check(token, inputs=INPUTS, **kw):
    return verify_approval_token(None, token, operator="crm", capability="crm.bank_change", inputs=inputs, mission_id="m-1",
                                 node_id="n-1", **kw)


def test_the_tenant_and_the_approver_are_signed_into_the_token():
    got = {}
    assert check(mint(), binding=got) and got == {"tenant": "acme", "approver": "approver:acme"}
    got = {}
    assert check(mint(tenant="globex", approver="bo"), binding=got) and got == {"tenant": "globex", "approver": "bo"}     # explicit beats marker
    got = {}
    assert check(mint(marker={"approved": True}), binding=got) and got == {"tenant": None, "approver": None}


def test_another_tenant_cannot_use_an_approval_and_a_refusal_does_not_spend_it():
    guard, token = ReplayGuard(), mint()
    assert check(token, caller_tenant="globex", replay_guard=guard) is None
    assert check(mint(marker={"approved": True}), caller_tenant="acme") is None          # unbound: no tenant may use it
    assert check(token, caller_tenant="acme", replay_guard=guard) is not None
    assert check(token, caller_tenant="acme", replay_guard=guard) is None                 # single use


def test_a_tenant_named_in_the_inputs_must_be_the_approved_one():
    inputs = {**INPUTS, "_tenant": "globex"}
    assert check(mint(inputs), inputs) is None
    assert check(mint({**INPUTS, "_tenant": "acme"}), {**INPUTS, "_tenant": "acme"}) is not None


def test_where_an_identity_is_required_an_anonymous_approval_is_refused(env):
    anonymous, named = mint(marker={"approved": True}), mint()
    assert not approver_identity_required() and check(anonymous) is not None
    env.setenv("AGENTIC_OS_PROFILE", "production")
    assert not approver_identity_required()                                              # production with one owner
    env.setenv("AGENTIC_OS_TENANT_LIMITS", '{"acme": {"policy_refs": []}}')
    assert approver_identity_required() and check(anonymous) is None and check(named) is not None
    env.delenv("AGENTIC_OS_PROFILE")
    env.setenv("AGENTIC_OS_REQUIRE_APPROVER_IDENTITY", "1")
    assert check(anonymous) is None


# ── at the operator's HTTP boundary ──────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def operator():
    seen = []

    def change(i):
        seen.append(dict(i))
        return {"changed": True}

    op = Operator("crm", [capability("crm.bank_change", change, side_effecting=True, approval_required=True, undo="crm.undo"),
                          capability("crm.undo", lambda i: {"undone": True}, side_effecting=True)])
    # this line's suite-wide operator policy also waives approvals; these tests are about approvals, so their operator keeps
    # a policy that supplies authority only (as main's fixture removes main's own suite-wide override)
    op.test_policy = AuthorityTestPolicy(name="approval-binding", reason="these tests are about whose approval it is, not authority",
                                         allow=("crm.*",), anonymous_operators=False)
    app = FastAPI()
    app.include_router(op.router())
    return TestClient(app, raise_server_exceptions=False), seen


def post(client, bearer, token, inputs=INPUTS, key="k"):
    return client.post("/invoke", json={"capability": "crm.bank_change", "inputs": inputs, "approval_token": token, "mission_id": "m-1",
                                        "node_id": "n-1", "idempotency_key": key}, headers={"Authorization": f"Bearer {bearer}"})


def test_one_tenants_sign_in_cannot_spend_another_tenants_approval(operator):
    client, seen = operator
    token = mint()
    r = post(client, GLOBEX, token, key="k1")
    assert seen == [] and r.json().get("result", {}).get("status") == "pending_approval"
    r = post(client, ACME, token, key="k2")
    assert r.status_code == 200 and len(seen) == 1 and seen[0]["_tenant"] == "acme" and seen[0]["_approval"]["by"] == "approver:acme"


def test_the_runtimes_call_runs_as_the_approved_tenant_not_a_claimed_one(operator):
    client, seen = operator
    assert post(client, ADMIN, mint(), key="k1").status_code == 200 and seen[-1]["_tenant"] == "acme"
    claimed = {**INPUTS, "_tenant": "globex"}
    post(client, ADMIN, mint(claimed), inputs=claimed, key="k2")                           # signed over a body that claims globex
    assert len(seen) == 1


# ── the runtime records who approved; an edit cannot say otherwise ───────────────────────────────────────────────────
def test_the_approver_and_tenant_in_the_marker_come_from_the_record_not_from_an_edit():
    from agentic_os.mission.runtime import MissionRuntime

    class Event:
        def __init__(self, payload):
            self.type, self.payload = "ApprovalGranted", payload

    class Mission:
        tenant = "acme"

    rt = object.__new__(MissionRuntime)
    rt._missions = {"m-1": Mission()}
    forged = {"by": "someone-else", "tenant": "globex", "note": "kept"}

    class Store:
        def __init__(self, payload):
            self.payload = payload

        def for_mission(self, _mid):
            return [Event(self.payload)]

    rt.store = Store({"node_id": "n-1", "approver": "approver:acme", "edit": forged})
    assert rt._approval_marker("m-1", "n-1") == {"approved": True, "note": "kept", "by": "approver:acme", "tenant": "acme"}
    rt.store = Store({"node_id": "n-1", "edit": forged})                                   # nobody recorded: nobody named
    assert rt._approval_marker("m-1", "n-1") == {"approved": True, "note": "kept", "tenant": "acme"}
