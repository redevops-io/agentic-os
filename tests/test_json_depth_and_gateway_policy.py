"""rc2 hardening of the Projects HTTP surface.

* G5: a JSON body nested deeper than 64 levels is refused with a controlled 400 before anything parses it (it used to be a 500 from
  ``RecursionError``), chunked or not, without buffering; legitimate bodies, strings full of brackets and non-JSON bodies are untouched.
* G4: the two agent-gateway POST routes have explicit route policies, present exactly when the gateway is mounted, and a gateway call with
  the right bearer still works under the production profile while a missing or wrong bearer is still refused.
"""
from __future__ import annotations

import base64
import json
import os
import secrets as _secrets

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agentic_os.app_kit import route_authz as ra
from agentic_os.app_kit.json_depth import MAX_JSON_DEPTH, JsonDepthScanner
from agentic_os.app_kit.route_authz import RoutePolicy as P, unclassified, write_routes
from agentic_os.projects_api import SampleProjectionProvider, create_app
from agentic_os.projects_route_policy import AGENT_GATEWAY_ROUTE_POLICIES, PROJECTS_ROUTE_POLICIES

GW = "gw-" + "x" * 24


def nest(n, opener="[", closer="]", inner=""):
    return opener * n + inner + closer * n


# ── the scanner ─────────────────────────────────────────────────────────────────────────────────────
def test_the_limit_is_exact():
    assert not JsonDepthScanner().feed(nest(MAX_JSON_DEPTH).encode())
    assert JsonDepthScanner().feed(nest(MAX_JSON_DEPTH + 1).encode())
    assert JsonDepthScanner().feed((b'{"a":' * (MAX_JSON_DEPTH + 1)))
    assert not JsonDepthScanner().feed(b'{"a":' * MAX_JSON_DEPTH)


@pytest.mark.parametrize("payload", [
    json.dumps({"text": "[" * 5000 + "{" * 5000}),                 # openers inside a string are not nesting
    json.dumps({"text": 'a "quoted" \\ ] } [ { thing'}),
    '{"k":"\\"' + "[" * 500 + '"}',                                # an escaped quote does not end the string
    json.dumps([[1, 2], [3, {"a": [4]}]]),
])
def test_brackets_in_strings_and_ordinary_nesting_are_fine(payload):
    assert not JsonDepthScanner().feed(payload.encode())


def test_unbalanced_closers_never_make_the_depth_negative():
    s = JsonDepthScanner()
    assert not s.feed(b"]}]}]}" * 100) and s.depth == 0
    assert JsonDepthScanner().feed(b"]]]]" + nest(MAX_JSON_DEPTH + 1).encode())


@pytest.mark.parametrize("step", [1, 2, 3, 7, 64])
def test_the_result_does_not_depend_on_how_the_body_is_chunked(step):
    for body, expect in ((nest(MAX_JSON_DEPTH + 1) + '"x', True), (('{"s":"' + "[" * 300 + '\\"[[' + '"}'), False),
                         (nest(MAX_JSON_DEPTH), False)):
        raw = body.encode()
        s = JsonDepthScanner()
        got = any(s.feed(raw[i:i + step]) for i in range(0, len(raw), step))
        assert got is expect, (body[:30], step)


# ── a bare app with the shared enforcement layer (what the apps use) ───────────────────────────────────
def bare():
    seen = []
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        seen.append(await request.body())
        return {"n": len(seen[-1])}

    ra.install(app, "demo", {"POST /echo": P("demo.echo", "read_only", rationale="test route")})
    return TestClient(app, raise_server_exceptions=True), seen


def test_a_deep_body_is_a_400_and_the_app_never_sees_it():
    c, seen = bare()
    for n in (65, 1000, 5000, 50_000):
        r = c.post("/echo", content=nest(n).encode(), headers={"content-type": "application/json"})
        assert r.status_code == 400 and r.json() == {"detail": "JSON nested too deeply"}, n
    assert not any(seen)                                                                   # at most an empty read after the disconnect
    assert c.post("/echo", json={"a": [1, {"b": [2]}]}).status_code == 200                # the next request is fine
    assert c.post("/echo", content=nest(MAX_JSON_DEPTH).encode(), headers={"content-type": "application/json"}).status_code == 200


def test_a_chunked_deep_body_is_refused_as_soon_as_the_limit_is_passed():
    c, seen = bare()
    sent = []

    def gen():
        for _ in range(2000):
            sent.append(1)
            yield b"[" * 10

    r = c.post("/echo", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 400 and not any(seen)


def test_text_and_json_like_content_types_are_inspected_in_any_case_and_with_any_method():
    c, seen = bare()
    for ctype in ("text/plain", "TEXT/PLAIN; charset=utf-8", "application/json", "Application/JSON; charset=utf-8",
                  "application/vnd.api+json", "application/ld+json", ""):
        h = {"content-type": ctype} if ctype else {}
        assert c.post("/echo", content=nest(70).encode(), headers=h).status_code == 400, ctype
    app = FastAPI()

    @app.api_route("/any", methods=["PUT", "PATCH", "DELETE"])
    async def any_(request: Request):
        return {"n": len(await request.body())}

    ra.install(app, "demo", {f"{m} /any": P("demo.any", "read_only", rationale="test route") for m in ("PUT", "PATCH", "DELETE")})
    c2 = TestClient(app, raise_server_exceptions=True)
    for m in ("PUT", "PATCH", "DELETE"):
        assert c2.request(m, "/any", content=nest(70).encode(), headers={"content-type": "application/json"}).status_code == 400, m
        assert c2.request(m, "/any", content=nest(10).encode(), headers={"content-type": "application/json"}).status_code == 200, m


def test_a_handler_that_parses_text_plain_as_json_cannot_be_driven_into_a_500():
    app = FastAPI()

    @app.post("/lenient")
    async def lenient(request: Request):
        return {"keys": len(await request.json())}                 # request.json() ignores the content type

    ra.install(app, "demo", {"POST /lenient": P("demo.lenient", "read_only", rationale="test route")})
    c = TestClient(app, raise_server_exceptions=True)
    assert c.post("/lenient", content=nest(5000).encode(), headers={"content-type": "text/plain"}).status_code == 400
    assert c.post("/lenient", content=b'{"a": 1}', headers={"content-type": "text/plain"}).status_code == 200


def test_other_content_types_are_not_inspected_and_a_get_is_never_inspected():
    c, seen = bare()
    for ctype in ("application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "application/octet-stream", "image/png"):
        r = c.post("/echo", content=nest(500).encode(), headers={"content-type": ctype})
        assert r.status_code == 200, ctype
    assert len([x for x in seen if x]) == 4
    seen.clear()
    r = c.post("/echo", content=nest(500).encode(), headers={"content-type": "application/vnd.api+json"})
    assert r.status_code == 400
    assert c.get("/echo").status_code == 405                                                # not a body method of the route; not our 400


# ── the Projects API under the production profile ─────────────────────────────────────────────────────
@pytest.fixture
def prod(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_REQUIRE_AUTHORITY", "AGENTIC_OS_SERVICE_TOKENS", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_TENANT_LIMITS",
              "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "OUTREACH_BASIC_AUTH", "AGENT_GATEWAY_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", _secrets.token_hex(24))
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-token")
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "ops:pw-" + _secrets.token_hex(6))
    return monkeypatch


def basic():
    return {"Authorization": "Basic " + base64.b64encode(os.environ["OPERATOR_BASIC_AUTH"].encode()).decode()}


def test_projects_write_routes_answer_a_deep_body_with_400_not_500(prod):
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=True)
    hdr = {**basic(), "content-type": "application/json"}
    for path in ("/api/projects/p1/capture", "/api/outcomes", "/api/sources/propose"):
        for n in (65, 1000, 5000):
            r = c.post(path, content=nest(n).encode(), headers=hdr)
            assert r.status_code == 400, (path, n, r.status_code)
        # unauthenticated callers get the same cheap refusal (before authorization), never a 500
        assert c.post(path, content=nest(1000).encode(), headers={"content-type": "application/json"}).status_code == 400
    assert c.post("/api/projects/p1/capture", json={"text": "call the customer back"}, headers=basic()).status_code not in (400, 401, 403, 500)


# ── G4: the agent-gateway routes have policies ──────────────────────────────────────────────────────────
@pytest.fixture
def dev(prod):
    prod.setenv("AGENTIC_OS_PROFILE", "development")
    return prod


def test_gateway_policies_exist_exactly_when_the_gateway_is_mounted(dev):
    app = create_app(SampleProjectionProvider())
    assert not [r for r in write_routes(app) if "agent-gateway" in r]
    assert not set(AGENT_GATEWAY_ROUTE_POLICIES) & set(app.state.route_policies)
    assert unclassified(app, app.state.route_policies) == [] and set(app.state.route_policies) == set(PROJECTS_ROUTE_POLICIES)

    dev.setenv("AGENT_GATEWAY_TOKEN", GW)
    app = create_app(SampleProjectionProvider())
    assert {r for r in write_routes(app) if "agent-gateway" in r} == set(AGENT_GATEWAY_ROUTE_POLICIES)
    assert unclassified(app, app.state.route_policies) == []                                  # nothing is left without a policy
    assert sorted(set(app.state.route_policies) - set(write_routes(app))) == []               # and no policy outlives its route
    assert all(p.access == "read_only" and p.rationale for p in AGENT_GATEWAY_ROUTE_POLICIES.values())


def test_the_route_layer_lets_a_gateway_call_through_to_the_gateways_own_bearer_check():
    anon = ra.AuthzContext(kind="anonymous") if hasattr(ra, "AuthzContext") else None
    assert anon is not None
    for pol in AGENT_GATEWAY_ROUTE_POLICIES.values():
        ok, status, _why, _marker = ra.decide(pol, anon, {}, app_name="projects")
        assert ok and status == 200          # read_only: the gateway, not this layer, authenticates the call


def test_gateway_calls_work_with_the_bearer_and_only_with_it(dev):
    dev.setenv("AGENT_GATEWAY_TOKEN", GW)
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=True)
    good = {"Authorization": f"Bearer {GW}"}
    caps = c.get("/api/agent-gateway/capabilities", headers=good)
    assert caps.status_code == 200
    body = caps.json()
    names = [x["name"] if isinstance(x, dict) else x for x in (body if isinstance(body, list) else body.get("capabilities", []))]
    assert names, caps.text
    ok = c.post(f"/api/agent-gateway/capabilities/{names[0]}/invoke", headers=good, json={})
    assert ok.status_code == 200, ok.text
    mcp = c.post("/api/agent-gateway/mcp", headers=good, json={"method": "tools/list", "params": {}})
    assert mcp.status_code == 200, mcp.text
    for bad in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Bearer {GW}x"}, {"Authorization": GW}):
        assert c.post(f"/api/agent-gateway/capabilities/{names[0]}/invoke", headers=bad, json={}).status_code == 401, bad
        assert c.post("/api/agent-gateway/mcp", headers=bad, json={"method": "tools/list", "params": {}}).status_code == 401, bad


def test_production_still_refuses_to_mount_the_gateway_without_a_durable_authority_store(prod):
    """Unchanged by rc2 and deliberately so: the policies above do not weaken this fail-closed startup rule."""
    from agentic_os.mission.profile import ProfileError
    prod.setenv("AGENT_GATEWAY_TOKEN", GW)
    with pytest.raises(ProfileError):
        create_app(SampleProjectionProvider())


def test_the_gateway_exposes_no_write_capability(dev):
    dev.setenv("AGENT_GATEWAY_TOKEN", GW)
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=True)
    r = c.get("/api/agent-gateway/capabilities", headers={"Authorization": f"Bearer {GW}"})
    blob = r.text.lower()
    assert "write" not in blob and "approve" not in blob and "delegate" not in blob
    # a deep body to the gateway is a 400 too (the guard is outside the gateway's own parsing)
    d = c.post("/api/agent-gateway/mcp", headers={"Authorization": f"Bearer {GW}", "content-type": "application/json"},
               content=nest(2000).encode())
    assert d.status_code == 400


# ── the two gateway POST routes are read-only (the basis for their read_only route policies) ───────────────
MUTATORS = {"connect_app", "remember_sources", "forget_source", "set_session_source_view", "record_outcome", "capture", "resolve_capture",
            "archive_capture", "today_batch", "today_approve", "today_dismiss", "today_snooze", "today_reconcile", "apply_batch"}


class Spy(SampleProjectionProvider):
    def __init__(self):
        super().__init__()
        object.__setattr__(self, "_calls", [])

    def __getattribute__(self, name):
        v = super().__getattribute__(name)
        if callable(v) and not name.startswith("_") and name != "__class__":
            super().__getattribute__("_calls").append(name)
        return v


def test_every_gateway_call_path_only_reads(dev):
    from agentic_os.agent_gateway.capabilities import READ_CAPABILITIES

    dev.setenv("AGENT_GATEWAY_TOKEN", GW)
    spy = Spy()
    app = create_app(spy)
    c = TestClient(app, raise_server_exceptions=True)
    good = {"Authorization": f"Bearer {GW}"}
    listed = c.get("/api/agent-gateway/capabilities", headers=good).json()["capabilities"]
    assert listed and all(x["risk_tier"] == 0 and x["side_effecting"] is False and x["approval"] == "auto" for x in listed)
    assert {x["name"] for x in listed} <= {m.name for m in READ_CAPABILITIES}
    before = json.dumps([spy.sources("customer-ops"), spy.connected and sorted(spy.connected)], sort_keys=True, default=str)
    spy._calls.clear()
    args = {"project_id": "customer-ops", "mission_id": "4821", "query": "x", "limit": 3}
    for x in listed:
        assert c.post(f"/api/agent-gateway/capabilities/{x['name']}/invoke", headers=good, json=args).status_code == 200
        assert c.post("/api/agent-gateway/mcp", headers=good, json={"method": "tools/call", "params": {"name": x["name"], "arguments": args}}).status_code == 200
    assert set(spy._calls) and not (set(spy._calls) & MUTATORS), spy._calls
    assert set(spy._calls) <= {"projects", "overview", "missions", "mission_detail", "attention", "sources", "workflows", "discovery", "apps",
                               "runtime", "templates", "workflows"}, set(spy._calls)
    spy._calls.clear()
    assert json.dumps([spy.sources("customer-ops"), spy.connected and sorted(spy.connected)], sort_keys=True, default=str) == before   # no state moved


def test_the_gateway_refuses_every_write_capability_by_name_and_every_mcp_method_but_two(dev):
    from agentic_os.agent_gateway import capabilities as caps

    dev.setenv("AGENT_GATEWAY_TOKEN", GW)
    spy = Spy()
    c = TestClient(create_app(spy), raise_server_exceptions=True)
    good = {"Authorization": f"Bearer {GW}"}
    writes = [m for m in vars(caps).values() if getattr(m, "side_effecting", False)]
    assert writes, "the module defines write capabilities that must stay unreachable here"
    spy._calls.clear()
    for m in writes:
        assert c.post(f"/api/agent-gateway/capabilities/{m.name}/invoke", headers=good, json={"goal": "x", "mission_id": "4821"}).json()["status"] == "denied", m.name
        r = c.post("/api/agent-gateway/mcp", headers=good, json={"method": "tools/call", "params": {"name": m.name, "arguments": {}}}).json()
        assert r.get("status") == "denied" or "error" in r, (m.name, r)
    for method in ("sampling/createMessage", "resources/read", "prompts/get", "tasks/create", "initialize", "tools/execute", ""):
        assert "error" in c.post("/api/agent-gateway/mcp", headers=good, json={"method": method, "params": {}}).json(), method
    assert not (set(spy._calls) & MUTATORS), spy._calls
