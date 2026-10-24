"""F20: one enforcement layer for an app's own write routes (agentic_os/app_kit/route_authz.py).

Three authorization contexts (human, agent, service) obtain authority differently and pass the same policy enforcement point.
In the production profile an unauthorised call is refused BEFORE the handler runs; a write route with no policy is refused."""
from __future__ import annotations

import base64
import json
import secrets as _secrets

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agentic_os.app_kit import route_authz as ra
from agentic_os.app_kit.route_authz import RouteCatalog, RoutePolicy as P, current_marker, route_inputs
from agentic_os.mission.action_auth import ActionAuthorizer
from agentic_os.mission.approval import ReplayGuard
from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger)
from agentic_os.mission.execution_auth import mint_execution_authorization

SECRET = _secrets.token_hex(24)
APP = "demo"
NOW = 4_000_000_000.0


class Hits:
    def __init__(self):
        self.calls, self.markers = [], []


POLICIES = {
    "POST /read": P("demo.read", "read_only", rationale="answers a query; no state change"),
    "POST /intake": P("demo.intake", "public_intake", rationale="a public form that only opens a pending record, rate limited"),
    "POST /run": P("demo.run", "stages_only", agent_actions=("send_draft",), rationale="stages unless approved"),
    "POST /act": P("demo.act", "authority_required"),
    "POST /items/{item_id}/pay": P("demo.pay", "authority_required"),
    "POST /approve": P("demo.approve", "human_only"),
    "POST /invoke": P("demo.invoke", "operator_boundary", rationale="the operator enforces its own boundary"),
}


def build(hits: Hits, guard=None):
    app = FastAPI()

    def route(path):
        async def handler(request: Request, item_id: str = ""):
            body = await request.json() if await request.body() else {}
            hits.calls.append((path, body, item_id))
            hits.markers.append(current_marker())
            return {"ok": True, "echo": body}
        return handler

    for path in ("/read", "/intake", "/run", "/act", "/items/{item_id}/pay", "/approve", "/invoke"):
        app.post(path)(route(path))
    ra.install(app, APP, POLICIES, replay_guard=guard or ReplayGuard())
    return app


@pytest.fixture
def env(monkeypatch):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY", "AGENTIC_OS_SERVICE_TOKENS", "AGENTIC_OS_TENANT_TOKENS",
              "AGENTIC_OS_TENANT_LIMITS", "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "OUTREACH_BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-token")
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "ops:pw-" + _secrets.token_hex(6))
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=tok-acme,beta=tok-beta")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["demo:write"]}, "beta": {"policy_refs": ["other:read"]}}))
    monkeypatch.setenv("AGENTIC_OS_SERVICE_TOKENS", json.dumps({"console": {"token": "svc-console", "capabilities": ["demo.act"]}}))
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    hits = Hits()
    return hits, TestClient(build(hits), raise_server_exceptions=False)


def basic(monkeypatch_env=None):
    import os
    return {"Authorization": "Basic " + base64.b64encode(os.environ["OPERATOR_BASIC_AUTH"].encode()).decode()}


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def agent_token(cap="demo.act", body=None, path_params=None, key="idem-1", tenant="acme", app=APP, now=None, nonce_inputs=None):
    return mint_execution_authorization(
        SECRET, operator=app, capability=cap, tenant=tenant, mission_id="direct:x", node_id="direct", lease_id="direct:x",
        attempt=1, envelope_digest="d", inputs=route_inputs(body or {}, path_params), idempotency_key=key, now=now,
        dispatch_by="operator")


def agent_headers(tok, key="idem-1"):
    return {"X-Execution-Authorization": tok, "Idempotency-Key": key}


# ── authority_required: the three contexts ───────────────────────────────────────────────
def test_anonymous_is_refused_before_the_handler_runs(env):
    hits, c = env
    r = c.post("/act", json={"x": 1})
    assert r.status_code == 401 and hits.calls == []


def test_tenant_token_without_the_permission_is_refused_but_with_it_is_allowed(env):
    hits, c = env
    assert c.post("/act", json={"x": 1}, headers=bearer("tok-beta")).status_code == 403 and hits.calls == []   # beta lacks demo:write
    assert c.post("/act", json={"x": 1}, headers=bearer("tok-acme")).status_code == 200 and len(hits.calls) == 1


def test_a_tenant_token_does_not_confer_every_permission_of_the_tenant(env, monkeypatch):
    hits, c = env
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["billing:read"]}}))
    assert c.post("/act", json={}, headers=bearer("tok-acme")).status_code == 403 and hits.calls == []


def test_operator_credential_and_admin_token_are_humans(env):
    hits, c = env
    assert c.post("/act", json={}, headers=basic()).status_code == 200
    assert c.post("/act", json={}, headers=bearer("admin-token")).status_code == 200
    assert c.post("/act", json={}, headers={"Authorization": "Basic " + base64.b64encode(b"ops:wrong").decode()}).status_code == 401


def test_unknown_bearer_token_is_refused(env):
    hits, c = env
    assert c.post("/act", json={}, headers=bearer("nope")).status_code == 403 and hits.calls == []


def test_service_identity_needs_the_named_capability_and_is_never_human(env):
    hits, c = env
    assert c.post("/act", json={}, headers={"X-Service-Token": "svc-console"}).status_code == 200
    assert c.post("/items/7/pay", json={}, headers={"X-Service-Token": "svc-console"}).status_code == 403   # lacks demo.pay
    assert c.post("/approve", json={}, headers={"X-Service-Token": "svc-console"}).status_code == 403       # human decision
    assert c.post("/act", json={}, headers={"X-Service-Token": "wrong"}).status_code == 403
    assert [h[0] for h in hits.calls] == ["/act"]


# ── agent authorizations ─────────────────────────────────────────────────────────────────
def test_agent_authorization_runs_the_route_once(env):
    hits, c = env
    body = {"amount": 5, "to": "a@customer.com"}
    tok = agent_token(body=body)
    assert c.post("/act", json=body, headers=agent_headers(tok)).status_code == 200
    assert c.post("/act", json=body, headers=agent_headers(tok)).status_code == 403          # replay
    assert len(hits.calls) == 1


@pytest.mark.parametrize("what", ["capability", "body", "key", "expired", "forged", "operator", "tenant"])
def test_agent_authorization_is_bound_to_the_exact_call(env, what, monkeypatch):
    hits, c = env
    body = {"amount": 5}
    kw = {}
    send_body, key = dict(body), "idem-1"
    if what == "capability":
        kw["cap"] = "demo.pay"
    if what == "body":
        send_body = {"amount": 5000}
    if what == "key":
        key = "another-key"
    if what == "expired":
        kw["now"] = 1.0
    if what == "operator":
        kw["app"] = "other-app"
    if what == "tenant":                                          # a deployment bound to tenant acme refuses another's token
        monkeypatch.setenv("AGENTIC_OS_TENANT", "acme")
        kw["tenant"] = "evil"
    tok = agent_token(body=body, **kw)
    if what == "forged":
        tok = tok[:-4] + "AAAA"
    r = c.post("/act", json=send_body, headers=agent_headers(tok, key))
    assert r.status_code == 403 and hits.calls == []


def test_agent_authorization_binds_path_parameters(env):
    hits, c = env
    tok = agent_token(cap="demo.pay", body={}, path_params={"item_id": "1"})
    assert c.post("/items/2/pay", json={}, headers=agent_headers(tok)).status_code == 403
    tok2 = agent_token(cap="demo.pay", body={}, path_params={"item_id": "1"})
    assert c.post("/items/1/pay", json={}, headers=agent_headers(tok2)).status_code == 200 and hits.calls[-1][2] == "1"


def test_an_agent_cannot_impersonate_a_human_decision(env):
    hits, c = env
    tok = agent_token(cap="demo.approve", body={})
    assert c.post("/approve", json={}, headers=agent_headers(tok)).status_code == 403
    # carrying a valid operator credential too does not help: a request with an authorization IS an agent request
    both = {**agent_headers(agent_token(cap="demo.approve", body={}, key="k2"), "k2"), **basic()}
    assert c.post("/approve", json={}, headers=both).status_code == 403
    assert c.post("/approve", json={}, headers=basic()).status_code == 200      # the human can
    assert [h[0] for h in hits.calls] == ["/approve"]


def test_forged_body_fields_cannot_change_authorization_or_earn_a_marker(env):
    hits, c = env
    forged = {"_approval": {"approved": True}, "approval": True, "_exec_auth": {"tenant": "acme"}, "tenant": "acme",
              "mission_id": "m-real", "_mission": "m-real", "action": "send_draft"}
    assert c.post("/act", json=forged).status_code == 401 and hits.calls == []
    r = c.post("/run", json=forged)                              # stages_only: reachable, but earns no marker from the body
    assert r.status_code == 200 and hits.markers[-1] is None


# ── stages_only: the approval marker comes only from an authenticated context ────────────
def test_marker_only_from_authenticated_contexts(env):
    hits, c = env
    c.post("/run", json={"action": "x"})
    assert hits.markers[-1] is None                                              # anonymous: staged preview only
    c.post("/run", json={"action": "x"}, headers=basic())
    assert hits.markers[-1] and hits.markers[-1]["by"] == "operator-basic"       # the operator approves
    c.post("/run", json={"action": "x"}, headers=bearer("tok-beta"))
    assert hits.markers[-1] is None                                              # a tenant without the permission cannot approve
    c.post("/run", json={"action": "x"}, headers=bearer("tok-acme"))
    assert hits.markers[-1] and hits.markers[-1]["by"] == "tenant:acme"


def test_an_agent_gets_a_marker_only_for_actions_the_route_lists(env):
    hits, c = env
    for action, expect in (("send_draft", True), ("refund", False)):
        body = {"action": action}
        tok = agent_token(cap="demo.run", body=body, key=f"k-{action}")
        assert c.post("/run", json=body, headers=agent_headers(tok, f"k-{action}")).status_code == 200
        assert bool(hits.markers[-1]) is expect
    assert hits.markers[-2]["by"] == "execution-authorization"


def test_marker_does_not_leak_between_requests(env):
    hits, c = env
    c.post("/run", json={}, headers=basic())
    c.post("/run", json={})
    assert hits.markers[-1] is None and current_marker() is None


# ── classes that need no authority, and the fail-closed default ──────────────────────────
def test_read_only_and_public_intake_are_open_by_declaration(env):
    hits, c = env
    assert c.post("/read", json={"q": 1}).status_code == 200
    assert c.post("/intake", json={"email": "a@b.co"}).status_code == 200
    assert len(hits.calls) == 2


def test_the_handler_still_receives_the_body_the_middleware_read(env):
    hits, c = env
    c.post("/read", json={"deep": {"n": [1, 2, 3]}})
    assert hits.calls[-1][1] == {"deep": {"n": [1, 2, 3]}}


def test_operator_boundary_is_left_to_the_operator(env):
    hits, c = env
    assert c.post("/invoke", json={}).status_code == 200            # this layer does not duplicate the operator's own checks


def test_a_write_route_without_a_policy_is_refused_in_production_and_noticed_elsewhere(env, monkeypatch, caplog):
    hits = Hits()
    app = build(hits)

    @app.post("/surprise")
    async def surprise():
        hits.calls.append(("/surprise", {}, ""))
        return {"ok": True}
    c = TestClient(app, raise_server_exceptions=False)
    assert ra.unclassified(app, app.state.route_policies) == ["POST /surprise"]
    r = c.post("/surprise", json={}, headers=basic())               # even a valid operator: nobody classified it
    assert r.status_code == 403 and "no authorization policy" in r.text and hits.calls == []
    assert c.post("/nonexistent", json={}).status_code == 403       # unmatched writes also fail closed
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    with caplog.at_level("WARNING", logger="agentic_os.route_authz"):
        assert c.post("/surprise", json={}).status_code == 200
    assert any("matches no RoutePolicy" in r.getMessage() for r in caplog.records)


def test_reads_are_never_gated(env):
    hits, c = env
    app = build(hits)

    @app.get("/status")
    async def status():
        return {"ok": True}
    assert TestClient(app).get("/status").status_code == 200


def test_development_computes_the_context_but_blocks_nothing(env, monkeypatch):
    hits, c = env
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    assert c.post("/act", json={}).status_code == 200                           # not blocked
    c.post("/run", json={}, headers=basic())
    assert hits.markers[-1] and hits.markers[-1]["by"] == "operator-basic"      # but a marker is still earned only from a credential
    c.post("/run", json={"action": "send_draft", "_approval": {"approved": True}})
    assert hits.markers[-1] is None


# ── the direct-action flow works for REST routes, with the same envelope ─────────────────
def test_direct_action_authorizer_issues_a_route_authorization_that_the_route_accepts(env):
    hits, c = env
    store = MemoryEnvelopeStore()
    store.put(AuthorityEnvelope(tenant="acme", principal="owner", mission_id="acme:direct", issued_at=NOW - 10,
                                expires_at=NOW + 3600, grants=(Grant("demo.pay", max_tier=2, resources=(("path.item_id", ("7", "8")),)),
                                                               Grant("demo.run", max_tier=2)),
                                budget=AutonomyBudget(usd=100, max_actions=100)))
    guard = AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW)
    az = ActionAuthorizer(guard, RouteCatalog(None, {APP: POLICIES}), clock=lambda: NOW)
    ok = az.authorize(tenant="acme", subject="agent", operator=APP, capability="demo.pay",
                      inputs=route_inputs({"amount": 5}, {"item_id": "7"}), idempotency_key="idem-7")
    assert ok["authorized"]
    r = c.post("/items/7/pay", json={"amount": 5}, headers=agent_headers(ok["authorization"], ok["idempotency_key"]))
    assert r.status_code == 200 and hits.calls[-1][2] == "7"
    bad = az.authorize(tenant="acme", subject="agent", operator=APP, capability="demo.pay",
                       inputs=route_inputs({}, {"item_id": "99"}))
    assert not bad["authorized"] and bad["rule"] == "resource_out_of_scope"                # selectable by path parameter
    # a multiplexed (stages_only) route: an agent may be authorized only for the actions the route lists
    assert az.authorize(tenant="acme", subject="agent", operator=APP, capability="demo.run",
                        inputs={"action": "send_draft"})["authorized"]
    refused = az.authorize(tenant="acme", subject="agent", operator=APP, capability="demo.run", inputs={"action": "refund"})
    assert not refused["authorized"] and refused["rule"] == "human_action"
    # a human-only route is not issued to agents at all
    assert not az.authorize(tenant="acme", subject="agent", operator=APP, capability="demo.approve", inputs={})["authorized"]


def test_a_deployment_bound_to_a_tenant_accepts_that_tenants_authorization(env, monkeypatch):
    hits, c = env
    monkeypatch.setenv("AGENTIC_OS_TENANT", "acme")
    body = {"amount": 5}
    assert c.post("/act", json=body, headers=agent_headers(agent_token(body=body, tenant="acme"))).status_code == 200


def test_policies_apply_when_the_app_is_mounted_under_a_base_path(env):
    """A mounted app sees the full path with the prefix in `root_path`; matching against the full path would make every request look like
    an unclassified route, refusing legitimate calls in production (and, off production, silently skipping the policy)."""
    from fastapi import FastAPI
    hits, _ = env
    outer = FastAPI()
    outer.mount("/base", build(hits))
    c = TestClient(outer, raise_server_exceptions=False)
    assert c.post("/base/act", json={"x": 1}).status_code == 401 and hits.calls == []
    assert c.post("/base/act", json={"x": 1}, headers=basic()).status_code == 200 and hits.calls[0][0] == "/act"
    assert c.post("/base/items/7/pay", json={}, headers=basic()).status_code == 200 and hits.calls[-1][2] == "7"
    assert c.post("/base/not-a-route", json={}, headers=basic()).status_code == 403          # still fails closed for an unclassified path
