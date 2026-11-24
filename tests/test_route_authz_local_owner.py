"""The desktop owner under the production profile.

The production profile enforces every route policy. The desktop app has one user and no operator token, so the local session (set on the ASGI
scope by the capability gate after its own checks) is that user's authentication. These tests pin both directions: the owner's writes and
decisions are allowed, and nothing else gains from it (no session = anonymous, an agent credential stays an agent, a rejected credential stays
rejected, the key cannot arrive from a client).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os import local_capability
from agentic_os.app_kit import route_authz
from agentic_os.app_kit.route_authz import AuthzContext, RoutePolicy, decide, local_owner
from agentic_os.products import principal


def test_the_scope_key_and_owner_name_are_the_same_everywhere():
    assert route_authz.LOCAL_SESSION_SCOPE_KEY == local_capability.SESSION_SCOPE_KEY == principal.SESSION_SCOPE_KEY
    assert route_authz.LOCAL_OWNER == principal.LOCAL_OWNER


def test_a_local_session_makes_an_anonymous_request_the_owner():
    ctx = local_owner({route_authz.LOCAL_SESSION_SCOPE_KEY: object()}, AuthzContext())
    assert (ctx.kind, ctx.subject, ctx.permissions) == ("human", "local-owner", ("*",))
    for access in ("authority_required", "human_only"):
        allowed, status, _, marker = decide(RoutePolicy("projects.capture", access), ctx, {}, app_name="projects")
        assert allowed and status == 200 and marker == {"approved": True, "by": "local-owner"}


def test_without_a_session_the_request_stays_anonymous_and_is_refused():
    ctx = local_owner({}, AuthzContext())
    assert ctx.kind == "anonymous"
    assert decide(RoutePolicy("projects.capture", "authority_required"), ctx, {}, app_name="projects")[:2] == (False, 401)


def test_another_credential_keeps_its_own_verdict():
    scope = {route_authz.LOCAL_SESSION_SCOPE_KEY: object()}
    agent = AuthzContext(kind="agent", subject="lease-1", claims={"lease_id": "lease-1"})
    assert local_owner(scope, agent) is agent                      # an execution authorization never becomes a person
    rejected = AuthzContext(credential_rejected=True)
    assert local_owner(scope, rejected) is rejected
    assert decide(RoutePolicy("x.y", "human_only"), rejected, {}, app_name="projects")[:2] == (False, 403)


def _app(with_gate_key: bool) -> FastAPI:
    app = FastAPI()

    @app.post("/api/thing")
    def thing():
        return {"ok": True}

    route_authz.install(app, "projects", {"POST /api/thing": RoutePolicy("projects.thing", "authority_required")})
    if with_gate_key:
        class Gate:                                                 # stands in for the capability gate: outermost, sets the key itself
            def __init__(self, inner):
                self.inner = inner

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    scope = {**scope, route_authz.LOCAL_SESSION_SCOPE_KEY: object()}
                await self.inner(scope, receive, send)
        app.add_middleware(Gate)
    return app


def test_production_enforces_and_the_owner_passes_while_a_bare_caller_does_not(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    assert TestClient(_app(True)).post("/api/thing", json={}).status_code == 200
    bare = TestClient(_app(False)).post("/api/thing", json={}, headers={"agentic_os.local_session": "x", "x-agentic-session": "x"})
    assert bare.status_code == 401                                 # a header is not the scope key
