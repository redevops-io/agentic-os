"""F19: the operator boundary. Authentication is not authority.

In the production profile an operator refuses a side-effecting invocation unless it carries a human approval or an
execution authorization issued AFTER the owner's envelope and policy were evaluated. The authorization is bound to the
exact call, single-use across replicas, and its mission identity comes from signed claims, never from the request body."""
from __future__ import annotations

import secrets as _secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.action_auth import ActionAuthorizer
from agentic_os.mission.approval import ReplayProtectionNotDurable, mint_approval_token
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, Lease, SqliteEnvelopeStore, SqliteLedger)
from agentic_os.mission.execution_auth import mint_execution_authorization
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import Operator, capability
from agentic_os.mission.operators import HTTPOperatorClient
from agentic_os.mission.policy import STANDING, Effect, MissionPolicy, PolicyRule
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState)

SECRET = _secrets.token_hex(24)                      # generated per run: no secret-looking literal in the repository
TOKEN = "control-token"
NOW = 4_000_000_000.0                                # far future: authorizations minted with real clocks stay valid


class Calls:
    def __init__(self):
        self.sent, self.fail = [], False


def make_operator(calls: Calls) -> Operator:
    def send(i):
        if calls.fail:
            raise RuntimeError("timeout after the request was written")
        calls.sent.append(dict(i))
        return {"sent": True, "to": i.get("recipient")}

    def delete(i):
        calls.sent.append({"deleted": i})
        return {"deleted": True}

    return Operator("crm", [
        capability("crm.send", send, provides=["done"], side_effecting=True, undo="crm.undo"),
        capability("crm.delete", delete, provides=["gone"], side_effecting=True, undo="crm.undo"),
        capability("crm.lookup", lambda i: {"found": True}, provides=["info"]),
        capability("crm.undo", lambda i: {"undone": True}, side_effecting=True),
        capability("crm.bank_change", lambda i: {"changed": True}, side_effecting=True, approval_required=True, undo="crm.undo"),
    ])


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    monkeypatch.setenv("AGENTIC_OS_AUTHORITY_DB", str(tmp_path / "auth.db"))
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    store = SqliteEnvelopeStore(str(tmp_path / "auth.db"))
    ledger = SqliteLedger(str(tmp_path / "auth.db") + ".usage")
    guard = AuthorityGuard(store, ledger, clock=lambda: NOW)
    return store, ledger, guard


def envelope(*grants, key="acme:direct", **kw):
    base = dict(tenant="acme", principal="owner", mission_id=key, issued_at=NOW - 10, expires_at=NOW + 3600,
                grants=grants, budget=AutonomyBudget(usd=1000, max_actions=1000))
    base.update(kw)
    return AuthorityEnvelope(**base)


def replica(calls):
    op = make_operator(calls)
    app = FastAPI()
    app.include_router(op.router())
    return op, TestClient(app, raise_server_exceptions=False)


def registry_for(op: Operator) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("crm", list(op.manifest.capabilities)))
    return reg


def invoke(client, capability_name, inputs, *, authorization=None, approval_token=None, mission_id=None, node_id=None,
           key="k1"):
    body = {"capability": capability_name, "inputs": inputs, "idempotency_key": key}
    if node_id:
        body["node_id"] = node_id
    if authorization:
        body["authorization"] = authorization
    if approval_token:
        body["approval_token"] = approval_token
    if mission_id:
        body["mission_id"] = mission_id
    return client.post("/invoke", json=body, headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key})


def direct(guard, op, cap, inputs, tenant="acme", **kw):
    return ActionAuthorizer(guard, registry_for(op), clock=lambda: NOW).authorize(
        tenant=tenant, subject="requester", operator="crm", capability=cap, inputs=inputs, **kw)


# ── the acceptance table ────────────────────────────────────────────────────────────────
def test_valid_operator_token_without_action_authority_is_denied(env):
    calls = Calls()
    _, c = replica(calls)
    r = invoke(c, "crm.send", {"recipient": "a@customer.com"})
    assert r.status_code == 403 and "execution authorization required" in r.text and calls.sent == []


def test_forged_mission_id_is_denied(env):
    calls = Calls()
    _, c = replica(calls)
    r = invoke(c, "crm.send", {"recipient": "a@customer.com", "_mission": "m-real"}, mission_id="m-real")
    assert r.status_code == 403 and calls.sent == []


def test_mission_identity_comes_from_the_signed_claims_not_the_body(env):
    store, _, guard = env
    store.put(envelope(Grant("crm.send")))
    calls = Calls()
    op, c = replica(calls)
    out = direct(guard, op, "crm.send", {"recipient": "a@customer.com"})
    assert out["authorized"]
    r = invoke(c, "crm.send", {"recipient": "a@customer.com", "_mission": "m-attacker"},
               authorization=out["authorization"], mission_id="m-attacker", key=out["idempotency_key"])
    assert r.status_code == 200 and calls.sent[-1]["_mission"] == out["lease_id"]       # claims won, the body lost


def test_token_reused_for_another_capability_is_denied(env):
    store, _, guard = env
    store.put(envelope(Grant("crm.send"), Grant("crm.delete")))
    calls = Calls()
    op, c = replica(calls)
    out = direct(guard, op, "crm.send", {"x": 1})
    r = invoke(c, "crm.delete", {"x": 1}, authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 403 and calls.sent == []


def test_token_reused_with_changed_recipient_or_amount_is_denied(env):
    store, _, guard = env
    store.put(envelope(Grant("crm.send")))
    calls = Calls()
    op, c = replica(calls)
    out = direct(guard, op, "crm.send", {"recipient": "a@customer.com", "amount": 100})
    for tampered in ({"recipient": "a@evil.com", "amount": 100}, {"recipient": "a@customer.com", "amount": 100000},
                     {"recipient": "a@customer.com"}):
        assert invoke(c, "crm.send", tampered, authorization=out["authorization"],
                      key=out["idempotency_key"]).status_code == 403
    assert calls.sent == []
    ok = invoke(c, "crm.send", {"recipient": "a@customer.com", "amount": 100}, authorization=out["authorization"],
                key=out["idempotency_key"])
    assert ok.status_code == 200 and len(calls.sent) == 1                                # the original, untampered call


def test_token_replayed_on_another_replica_is_denied(env):
    store, _, guard = env
    store.put(envelope(Grant("crm.send")))
    calls_a, calls_b = Calls(), Calls()
    op_a, ca = replica(calls_a)
    op_b, cb = replica(calls_b)                                                         # same durable replay db + ledger
    out = direct(guard, op_a, "crm.send", {"recipient": "a@customer.com"})
    first = invoke(ca, "crm.send", {"recipient": "a@customer.com"}, authorization=out["authorization"], key=out["idempotency_key"])
    second = invoke(cb, "crm.send", {"recipient": "a@customer.com"}, authorization=out["authorization"], key=out["idempotency_key"])
    assert first.status_code == 200 and second.status_code == 403
    assert len(calls_a.sent) + len(calls_b.sent) == 1


def test_expired_authorization_is_denied(env):
    store, _, guard = env
    store.put(envelope(Grant("crm.send")))
    calls = Calls()
    op, c = replica(calls)
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.send", tenant="acme", mission_id="direct:x",
                                       node_id="direct", lease_id="direct:x", attempt=1, envelope_digest="d",
                                       inputs={"a": 1}, idempotency_key="k1", ttl_s=60, now=1.0, dispatch_by="runtime")
    assert invoke(c, "crm.send", {"a": 1}, authorization=tok).status_code == 403 and calls.sent == []


def test_direct_user_action_with_scoped_authorization_executes_without_a_mission(env):
    store, ledger, guard = env
    store.put(envelope(Grant("crm.send", resources=(("recipient", ("*@customer.com",)),))))
    calls = Calls()
    op, c = replica(calls)
    out = direct(guard, op, "crm.send", {"recipient": "a@customer.com"})
    assert out["authorized"] and out["lease_id"].startswith("direct:")
    r = invoke(c, "crm.send", {"recipient": "a@customer.com"}, authorization=out["authorization"], key=out["idempotency_key"])
    assert r.status_code == 200 and r.json()["result"]["sent"] is True
    assert ledger.lease_state(out["lease_id"]) == Lease.SETTLED.value                    # the operator settled it
    refused = direct(guard, op, "crm.send", {"recipient": "a@evil.com"})                 # outside the granted resource
    assert not refused["authorized"] and refused["rule"] == "resource_out_of_scope"
    assert direct(guard, op, "crm.delete", {})["rule"] == "out_of_scope"                 # not granted at all


def test_unused_direct_authorization_costs_nothing_once_swept_and_can_never_be_used(env):
    store, ledger, guard = env
    store.put(envelope(Grant("crm.send", max_calls=1)))
    calls = Calls()
    op, c = replica(calls)
    out = direct(guard, op, "crm.send", {"a": 1})
    assert ledger.lease_state(out["lease_id"]) == Lease.RESERVED.value                   # not dispatched: nothing left yet
    assert not direct(guard, op, "crm.send", {"a": 2})["authorized"]                    # allowance held while it is live
    assert guard.sweep_undispatched(0) == 1                                              # operator releases the stale one
    assert direct(guard, op, "crm.send", {"a": 3})["authorized"]                        # capacity is back
    late = invoke(c, "crm.send", {"a": 1}, authorization=out["authorization"], key=out["idempotency_key"])
    assert late.status_code == 403 and calls.sent == []                                  # a released lease cannot be dispatched


def test_mandatory_approval_capabilities_are_never_directly_authorized(env):
    store, _, guard = env
    store.put(envelope(Grant("crm.bank_change", max_tier=3)))
    op, _ = replica(Calls())
    out = direct(guard, op, "crm.bank_change", {"iban": "X"})
    assert not out["authorized"] and out["rule"] == "mandatory_approval"


def test_authorization_cannot_pass_as_an_approval_nor_the_reverse(env):
    store, _, guard = env
    store.put(envelope(Grant("crm.send")))
    calls = Calls()
    op, c = replica(calls)
    out = direct(guard, op, "crm.send", {"a": 1})
    assert invoke(c, "crm.send", {"a": 1}, approval_token=out["authorization"], key=out["idempotency_key"]).status_code == 403
    appr = mint_approval_token(SECRET, operator="crm", capability="crm.send", mission_id="m1", node_id="n1",
                               inputs={"a": 1}, marker={"approved": True})
    assert invoke(c, "crm.send", {"a": 1}, authorization=appr, key="k9").status_code == 403
    assert calls.sent == []


def test_human_approval_still_authorizes_in_production(env):
    calls = Calls()
    seen = []
    op, c = replica(calls)
    op._handlers["crm.send"] = lambda i: (seen.append(i.get("_approval")), {"sent": True})[1]
    tok = mint_approval_token(SECRET, operator="crm", capability="crm.send", mission_id="m1", node_id="n1",
                              inputs={"a": 1}, marker={"approved": True})
    r = invoke(c, "crm.send", {"a": 1}, approval_token=tok, mission_id="m1", node_id="n1", key="k5")
    assert r.status_code == 200 and seen == [{"approved": True}]                          # the human's approval reached the handler
    replay = invoke(c, "crm.send", {"a": 1}, approval_token=tok, mission_id="m1", node_id="n1", key="k6")
    assert replay.status_code == 403                                                      # single-use: a replay is refused


def test_a_tenant_cannot_use_another_tenants_authorization(env, monkeypatch):
    store, _, guard = env
    store.put(envelope(Grant("crm.send")))
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "evil=evil-token")
    calls = Calls()
    op, c = replica(calls)
    out = direct(guard, op, "crm.send", {"a": 1})                                         # issued for tenant acme
    r = c.post("/invoke", json={"capability": "crm.send", "inputs": {"a": 1}, "authorization": out["authorization"],
                                "idempotency_key": out["idempotency_key"]},
               headers={"Authorization": "Bearer evil-token", "Idempotency-Key": out["idempotency_key"]})
    assert r.status_code == 403 and calls.sent == []


def test_reads_need_only_authentication_in_production(env):
    _, c = replica(Calls())
    r = invoke(c, "crm.lookup", {})
    assert r.status_code == 200 and r.json()["result"]["found"] is True


def test_development_profile_is_unchanged(env, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    calls = Calls()
    _, c = replica(calls)
    assert invoke(c, "crm.send", {"recipient": "a@customer.com"}).status_code == 200 and len(calls.sent) == 1


def test_production_operator_cannot_start_without_a_durable_replay_store(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.delenv("AGENTIC_OS_REPLAY_DB", raising=False)
    with pytest.raises(ReplayProtectionNotDurable):
        make_operator(Calls())


# ── the mission runtime drives the same boundary (standing grant, no human) ─────────────
class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome="done", need="do it")])


def runtime(env, op, c, *grants, policy_mode=STANDING):
    store, _, guard = env
    store.put(envelope(*grants, key="env-1"))

    def transport(url, body, headers, timeout):
        resp = c.post("/invoke", json=body, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"operator refused: {resp.status_code} {resp.text[:120]}")
        return resp.json()

    rt = MissionRuntime(registry_for(op), Executor(HTTPOperatorClient({"crm": "http://crm"}, transport=transport)),
                        planner=_P(), authority=guard, authority_key=lambda m: "env-1")
    m = rt.create_mission("send it", policy_refs=["*"], tenant="acme", principal="owner",
                          policy=MissionPolicy(id="p", version="1", rules=(
                              PolicyRule("w", Effect.REQUIRE_APPROVAL, "side_effecting", approval_mode=policy_mode),)))
    return rt, m, guard


def test_standing_grant_executes_over_http_without_a_human_and_is_settled(env):
    calls = Calls()
    op, c = replica(calls)
    rt, m, guard = runtime(env, op, c, Grant("crm.send"))
    assert rt.run(m.id).state == MissionState.SUCCEEDED and len(calls.sent) == 1
    assert calls.sent[0]["_mission"] == m.id                                              # derived from the signed claims
    assert guard.outstanding("env-1") == []


def test_timeout_after_a_possible_side_effect_is_unknown_and_no_second_authorization_exists(env):
    store, ledger, guard = env
    calls = Calls()
    calls.fail = True
    op, c = replica(calls)
    rt, m, guard = runtime(env, op, c, Grant("crm.send", max_calls=3))
    assert rt.run(m.id).state == MissionState.FAILED
    out = guard.outstanding("env-1")
    assert len(out) == 1 and out[0][1] == Lease.UNKNOWN.value                             # counted, needs reconciliation
    node = [n for n in rt._plans[m.id].graph.nodes][0]
    assert rt._lease_dispatched(m, node) is None                                          # a retry gets NO new authorization
    assert guard.ledger.lease_dispatch_once(out[0][0]) is False
    assert invoke(c, "crm.send", {"a": 1}).status_code == 403                             # and a bare call is refused
    assert guard.reconcile(out[0][0], executed=False) and guard.outstanding("env-1") == []


def test_a_mission_without_an_envelope_cannot_reach_the_operator(env):
    calls = Calls()
    op, c = replica(calls)
    store, _, guard = env
    rt = MissionRuntime(registry_for(op), Executor(HTTPOperatorClient({"crm": "http://crm"}, transport=lambda u, b, h, t: c.post("/invoke", json=b, headers=h).json())),
                        planner=_P(), authority=guard, authority_key=lambda m: "none")
    m = rt.create_mission("send it", policy_refs=["*"], tenant="acme", principal="owner")
    assert rt.run(m.id).outcome["blocked"] == "authority_denied" and calls.sent == []


def test_runtime_issued_authorization_replayed_on_another_replica_is_denied_by_nonce_consumption(env):
    """A runtime token has no operator-side dispatch (the runtime already dispatched), so the durable, shared nonce
    store is its only replay defence. Two replicas, one token, one effect."""
    calls_a, calls_b = Calls(), Calls()
    _, ca = replica(calls_a)
    _, cb = replica(calls_b)                                      # different process memory, same durable replay db
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.send", tenant="acme", mission_id="m-1",
                                       node_id="n-1", lease_id="m-1:n-1:a1", attempt=1, envelope_digest="d",
                                       inputs={"recipient": "a@customer.com"}, idempotency_key="idem-1",
                                       now=None, dispatch_by="runtime")
    first = invoke(ca, "crm.send", {"recipient": "a@customer.com"}, authorization=tok, key="idem-1")
    second = invoke(cb, "crm.send", {"recipient": "a@customer.com"}, authorization=tok, key="idem-1")
    assert first.status_code == 200 and second.status_code == 403
    assert len(calls_a.sent) + len(calls_b.sent) == 1


def test_authorization_is_bound_to_the_idempotency_key(env):
    calls = Calls()
    _, c = replica(calls)
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.send", tenant="acme", mission_id="m-1",
                                       node_id="n-1", lease_id="m-1:n-1:a1", attempt=1, envelope_digest="d",
                                       inputs={"a": 1}, idempotency_key="idem-1", dispatch_by="runtime")
    assert invoke(c, "crm.send", {"a": 1}, authorization=tok, key="a-different-key").status_code == 403
    assert calls.sent == []


def test_an_execution_authorization_can_never_satisfy_a_mandatory_approval_capability(env):
    calls = Calls()
    op, c = replica(calls)
    op._handlers["crm.bank_change"] = lambda i: (calls.sent.append(i), {"changed": True})[1]
    tok = mint_execution_authorization(SECRET, operator="crm", capability="crm.bank_change", tenant="acme", mission_id="m-1",
                                       node_id="n-1", lease_id="m-1:n-1:a1", attempt=1, envelope_digest="d",
                                       inputs={"iban": "X"}, idempotency_key="k1", dispatch_by="runtime")
    r = invoke(c, "crm.bank_change", {"iban": "X"}, authorization=tok, key="k1")
    assert r.status_code == 403 and "human approval" in r.text and calls.sent == []


def test_operator_reports_its_effective_security_posture(env, monkeypatch):
    _, c = replica(Calls())
    prod = c.get("/_security").json()
    assert prod["profile"] == "production" and prod["explicit"] is True and prod["authority_durable"] is True
    assert prod["replay_durable"] is True and prod["ok"] is True and prod["warnings"] == []
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    dev = c.get("/_security").json()
    assert dev["profile"] == "development" and dev["ok"] is False and any("not enforced" in w for w in dev["warnings"])
