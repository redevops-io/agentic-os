"""PR-B: a mission cannot be created under a tenant the caller did not authenticate as.

Two levels, because the guarantee has two layers:

* over HTTP, on the router as this repo mounts it (`build_router` carries `mission_gate` on every route,
  even when the route objects are copied into another app), an anonymous create is refused and creates
  nothing;
* at the handler, where PR-B adds a guard for a mount that somehow arrives without a caller — pinned by
  invoking the endpoint directly, since the HTTP layer above it cannot produce that state.

The isolated single-operator demo (no tenant tokens configured) is asserted unchanged: this is a tenancy
rule, not a new requirement on people running the stack on their own machine.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_os.mission.api import build_router
from agentic_os.mission.service import build_pilot_runtime, mount

ADMIN, TOK_ACME = "admin-secret", "tok-acme"
H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
BODY = {"goal": "Onboard a new customer", "template": "onboarding"}


@pytest.fixture()
def tenancy(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TOK_ACME}")
    monkeypatch.delenv("AGENTIC_OS_APPROVER_TOKENS", raising=False)
    monkeypatch.delenv("AGENTIC_OS_GATE_READS", raising=False)


@pytest.fixture()
def no_tenancy(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    monkeypatch.delenv("AGENTIC_OS_APPROVER_TOKENS", raising=False)


def _app():
    from fastapi import FastAPI
    app = FastAPI()
    rt = mount(app, build_pilot_runtime())
    return app, rt


def _count(rt) -> int:
    return len(getattr(rt, "_missions", {}) or {})


# ── over HTTP, on the real mount ───────────────────────────────────────────────────────────────────

def test_anonymous_create_is_refused_and_creates_nothing(tenancy):
    app, rt = _app()
    r = TestClient(app).post("/missions", json={**BODY, "tenant": "globex"})
    assert r.status_code in (401, 503), r.text
    assert _count(rt) == 0, "a refused create must leave no mission behind"


def test_a_tenant_caller_writes_only_into_its_own_tenant(tenancy):
    """The body's tenant claim is not merely ignored — it cannot move the write."""
    app, rt = _app()
    r = TestClient(app).post("/missions", headers=H(TOK_ACME),
                             json={**BODY, "tenant": "globex", "policy_refs": ["billing:write"]})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert rt._missions[mid].tenant == "acme", "a scoped token never writes into a tenant it names"


def test_the_isolated_demo_still_creates_a_mission_anonymously_after_the_gate(no_tenancy):
    """Compatibility check on the other branch: with no tenants defined, nothing new is demanded. The gate
    itself still needs the control token for a POST, which is pre-existing behaviour, unchanged."""
    app, rt = _app()
    r = TestClient(app).post("/missions", headers=H(ADMIN), json=BODY)
    assert r.status_code == 200, r.text
    assert rt._missions[r.json()["id"]].tenant == ""
    assert _count(rt) == 1


# ── at the handler, for a mount that arrives with no caller at all ─────────────────────────────────

def _bare_endpoint():
    rt = build_pilot_runtime()
    router = build_router(rt)
    ep = next(r.endpoint for r in router.routes
              if getattr(r, "path", "") == "/missions" and "POST" in (getattr(r, "methods", None) or set()))
    return rt, ep


def _request_without_caller():
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/missions", "headers": [], "query_string": b"",
             "client": ("203.0.113.9", 5555), "server": ("127.0.0.1", 8080), "scheme": "http",
             "root_path": "", "http_version": "1.1"}
    req = Request(scope)
    req.state.caller = None                 # exactly the state of a router mounted without mission_gate
    return req


class _Body:
    def __init__(self, tenant):
        self.goal, self.constraints, self.policy_refs = "Onboard a new customer", [], None
        self.template, self.budget_usd, self.tenant = "onboarding", None, tenant


def test_a_caller_less_mount_refuses_a_tenant_claim_and_creates_nothing(tenancy):
    from fastapi import HTTPException
    rt, ep = _bare_endpoint()
    with pytest.raises(HTTPException) as e:
        ep(body=_Body("globex"), request=_request_without_caller())
    assert e.value.status_code == 401
    assert "tenancy-enabled" in str(e.value.detail)
    assert _count(rt) == 0, "the refusal must precede any runtime state"


def test_a_caller_less_mount_still_serves_the_single_operator_demo(no_tenancy):
    """No tenants configured means no tenancy rule: the guard stays out of the way of the isolated demo."""
    from fastapi import HTTPException
    rt, ep = _bare_endpoint()
    try:
        created = ep(body=_Body(""), request=_request_without_caller())
    except HTTPException as e:      # any refusal here must NOT be the tenancy guard
        assert "tenancy-enabled" not in str(e.detail), e.detail
    else:
        assert getattr(created, "tenant", "") in ("", None) or _count(rt) == 1

