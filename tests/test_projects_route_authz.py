"""F22: the Projects API's write routes sit behind the same enforcement layer as the apps'. In the production profile an unauthenticated call is
refused BEFORE any handler runs, a decision that belongs to a person is never an agent's or a service's, and a new write route with no
policy is refused; outside production the demo keeps working."""
from __future__ import annotations

import base64
import json
import re
import secrets as _secrets

import pytest
from fastapi.testclient import TestClient

from agentic_os.app_kit.route_authz import unclassified, write_routes
from agentic_os.projects_api import SampleProjectionProvider, create_app, mounted_app
from agentic_os.projects_route_policy import PROJECTS_ROUTE_POLICIES as POLICIES

HUMAN_ONLY = {"POST /api/apps/{provider}/connect", "POST /api/apps/{provider}/connect/start", "POST /api/sources/confirm",
              "POST /api/projects/{project_id}/policy/promotion-cycle", "POST /api/projects/{project_id}/today/batch",
              "POST /api/projects/{project_id}/today/{item_id}/approve", "POST /api/projects/{project_id}/today/{item_id}/reconcile",
              "POST /api/projects/{project_id}/today/{item_id}/simulate-outcome"}


class Spy(SampleProjectionProvider):
    def __init__(self):
        super().__init__()
        object.__setattr__(self, "_log", [])

    def __getattribute__(self, name):
        v = super().__getattribute__(name)
        if callable(v) and not name.startswith("_") and name != "__class__":
            super().__getattribute__("_log").append(name)
        return v


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY", "AGENTIC_OS_SERVICE_TOKENS", "AGENTIC_OS_TENANT_TOKENS",
              "AGENTIC_OS_TENANT_LIMITS", "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "OUTREACH_BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", _secrets.token_hex(24))
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))      # production requires durable replay protection
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-token")
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "ops:pw-" + _secrets.token_hex(6))
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=tok-acme,beta=tok-beta")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["projects:write"]}, "beta": {"policy_refs": ["other:read"]}}))
    monkeypatch.setenv("AGENTIC_OS_SERVICE_TOKENS", json.dumps({"console": {"token": "svc-console", "capabilities": ["projects.capture"]}}))
    return monkeypatch


def basic():
    import os
    return {"Authorization": "Basic " + base64.b64encode(os.environ["OPERATOR_BASIC_AUTH"].encode()).decode()}


def concrete(route: str) -> tuple:
    method, path = route.split(" ", 1)
    return method, re.sub(r"\{(\w+)\}", lambda m: {"provider": "twenty", "project_id": "p1", "item_id": "i1", "capture_id": "c1"}[m.group(1)], path)


def client(env, *, production=True, provider=None, **kw):
    env.setenv("AGENTIC_OS_PROFILE", "production" if production else "development")
    return TestClient(create_app(provider or Spy(), **kw), raise_server_exceptions=False)


# ───────────────────────── the registry ─────────────────────────
def test_every_write_route_has_a_policy_and_no_policy_is_stale():
    app = create_app(SampleProjectionProvider())
    assert unclassified(app, POLICIES) == []
    assert sorted(set(POLICIES) - set(write_routes(app))) == []                       # a policy for a route that no longer exists is a bug


def test_decisions_that_belong_to_a_person_are_human_only_and_everything_else_is_not_left_open():
    assert {k for k, p in POLICIES.items() if p.access == "human_only"} == HUMAN_ONLY
    assert all(p.access in ("read_only", "authority_required", "human_only") for p in POLICIES.values())
    assert [k for k, p in POLICIES.items() if p.access == "read_only"] == ["POST /api/sources/propose"]
    assert all(p.rationale for k, p in POLICIES.items() if p.access in ("read_only", "human_only"))


# ───────────────────────── production refuses before any handler runs ─────────────────────────
@pytest.mark.parametrize("route", sorted(r for r, p in POLICIES.items() if p.access != "read_only"))
def test_an_unauthenticated_write_is_refused_before_the_handler_runs(env, route):
    spy = Spy()
    c = client(env, provider=spy)
    spy._log.clear()
    method, path = concrete(route)
    r = c.request(method, path, json={})
    assert r.status_code in (401, 403), (route, r.status_code)
    assert spy._log == []                                                              # no provider method was even reached


def test_a_read_only_post_is_still_reachable_without_credentials(env):
    c = client(env)
    r = c.post("/api/sources/propose", json={"project_id": "p1", "text": "our CRM and the shared drive"})
    assert r.status_code not in (401, 403)


def test_an_operator_credential_reaches_authority_required_routes(env):
    spy = Spy()
    c = client(env, provider=spy)
    r = c.post("/api/projects/p1/capture", json={"text": "call the customer back"}, headers=basic())
    assert r.status_code not in (401, 403)


def test_a_wrong_operator_credential_is_refused(env):
    c = client(env)
    h = {"Authorization": "Basic " + base64.b64encode(b"ops:wrong").decode()}
    assert c.post("/api/projects/p1/capture", json={"text": "x"}, headers=h).status_code == 401


def test_tenant_tokens_need_the_projects_write_permission(env):
    c = client(env)
    assert c.post("/api/projects/p1/capture", json={"text": "x"}, headers={"Authorization": "Bearer tok-beta"}).status_code == 403
    assert c.post("/api/projects/p1/capture", json={"text": "x"}, headers={"Authorization": "Bearer tok-acme"}).status_code not in (401, 403)


def test_a_service_identity_gets_only_its_capability_and_never_a_human_decision(env):
    spy = Spy()
    c = client(env, provider=spy)
    svc = {"X-Service-Token": "svc-console"}
    assert c.post("/api/projects/p1/capture", json={"text": "x"}, headers=svc).status_code not in (401, 403)
    spy._log.clear()
    assert c.post("/api/projects/p1/today/i1/dismiss", json={}, headers=svc).status_code == 403      # lacks projects.today_dismiss
    for route in sorted(HUMAN_ONLY):
        method, path = concrete(route)
        assert c.request(method, path, json={}, headers=svc).status_code == 403, route
    assert spy._log == []


def test_an_agents_execution_authorization_can_never_make_a_human_decision(env):
    from agentic_os.app_kit.route_authz import route_inputs
    from agentic_os.mission.execution_auth import mint_execution_authorization
    import os
    spy = Spy()
    c = client(env, provider=spy)
    body, params = {}, {"project_id": "p1", "item_id": "i1"}
    tok = mint_execution_authorization(os.environ["AGENTIC_OS_APPROVAL_SECRET"], operator="projects", capability="projects.today_approve",
                                       tenant="acme", mission_id="direct:x", node_id="direct", lease_id="direct:x", attempt=1,
                                       envelope_digest="d", inputs=route_inputs(body, params), idempotency_key="k1", dispatch_by="operator")
    spy._log.clear()
    r = c.post("/api/projects/p1/today/i1/approve", json=body, headers={"X-Execution-Authorization": tok, "Idempotency-Key": "k1"})
    assert r.status_code == 403 and spy._log == []


def test_a_route_added_without_a_policy_is_refused_even_for_the_operator(env):
    env.setenv("AGENTIC_OS_PROFILE", "production")
    app = create_app(Spy())

    @app.post("/api/projects/{project_id}/secret-new-write")
    def new_write(project_id: str):
        return {"ran": True}
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/projects/p1/secret-new-write", json={}, headers=basic())
    assert r.status_code in (403, 404, 500) and "ran" not in r.text


def test_the_policies_hold_when_the_api_is_mounted_under_a_base_path(env):
    env.setenv("AGENTIC_OS_PROFILE", "production")
    c = TestClient(mounted_app("/projects", Spy()), raise_server_exceptions=False)
    assert c.post("/projects/api/projects/p1/capture", json={"text": "x"}).status_code in (401, 403)
    assert c.post("/projects/api/projects/p1/capture", json={"text": "x"}, headers=basic()).status_code not in (401, 403)
    assert c.post("/projects/api/projects/p1/today/i1/approve", json={}).status_code in (401, 403)


# ───────────────────────── nothing changes for the demo ─────────────────────────
@pytest.mark.parametrize("profile", ["development", "test"])
def test_outside_production_the_demo_keeps_working_without_credentials(env, profile):
    env.setenv("AGENTIC_OS_PROFILE", profile)
    c = TestClient(create_app(Spy()), raise_server_exceptions=False)
    r = c.post("/api/projects/p1/capture", json={"text": "call the customer back"})
    assert r.status_code not in (401, 403)
    assert c.post("/api/projects/p1/today/i1/snooze", json={"seconds": 60}).status_code not in (401, 403)


def test_reads_are_not_touched_by_the_layer(env):
    c = client(env)
    assert c.get("/api/health").status_code != 401 if c.get("/api/health").status_code != 404 else True
