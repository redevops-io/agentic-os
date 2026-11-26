"""One token per external agent: its own grants, expiry and revocation; what the sign-in gate lets it reach."""
import base64
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from agentic_os.agent_gateway import agent_tokens as AT
from agentic_os.projects_api import SampleProjectionProvider, create_app

ENV = ("AGENTIC_OS_PROFILE", "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_AGENTS_FILE", "AGENT_GATEWAY_AUDIT_PATH", "AGENT_GATEWAY_MCP",
       "PROJECTS_OPERATOR_GATE", "OPERATOR_BASIC_AUTH", "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "OUTREACH_BASIC_AUTH",
       "AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_DASHBOARDS",
       "PROJECTS_CORS_ORIGINS")


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html>secret ui</html>", encoding="utf-8")
    monkeypatch.setenv("PROJECTS_UI_DIR", str(ui))
    return monkeypatch


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def _names(r):
    body = r.json()
    items = body.get("capabilities", body) if isinstance(body, dict) else body
    return sorted(x["name"] for x in items)


def test_a_token_is_shown_once_and_only_its_hash_is_kept(tmp_path):
    f = str(tmp_path / "agents.json")
    token = AT.issue(f, "laptop", ["projects.read"], days=1)
    text = open(f, encoding="utf-8").read()
    assert token.startswith("aga_") and token not in text and AT.digest(token) in text
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    for bad in (dict(grants=["projects.write"], days=1), dict(grants=[], days=1), dict(grants=["projects.read"], days=0), dict(grants=["projects.read"], days=999)):
        with pytest.raises(ValueError):
            AT.issue(f, "other", **bad)
    with pytest.raises(ValueError, match="already exists"):
        AT.issue(f, "laptop", ["projects.read"], days=1)


def test_expiry_and_revocation_take_effect_at_once(tmp_path):
    f, now = str(tmp_path / "agents.json"), [1_000_000.0]
    store = AT.AgentTokenStore(f, clock=lambda: now[0])
    token = AT.issue(f, "laptop", ["projects.read"], days=1, now=now[0])
    who = store.verify(token)
    assert who.principal.id == "agent:laptop" and who.client_id == "laptop" and who.scopes == ("projects.read",)
    assert store.authorize(who.principal, "projects.read") and not store.authorize(who.principal, "sources.read")
    assert store.verify(token + "x") is None and store.verify("aga_wrong") is None and store.verify("") is None
    now[0] += 86_401
    assert store.verify(token) is None and not store.authorize(who.principal, "projects.read")
    now[0] -= 86_401
    assert AT.revoke(f, "laptop") is True
    assert store.verify(token) is None and not store.authorize(who.principal, "projects.read")     # even a principal already resolved


@pytest.mark.parametrize("damage", ["{broken", "[]", json.dumps({"schema": "other", "agents": []}), "DELETE", "DUPLICATE"])
def test_a_file_that_cannot_be_trusted_authenticates_nobody_not_the_previous_contents(tmp_path, damage):
    f = str(tmp_path / "agents.json")
    store = AT.AgentTokenStore(f)
    token = AT.issue(f, "laptop", ["projects.read"], days=1)
    assert store.verify(token) is not None
    if damage == "DELETE":
        os.unlink(f)
    elif damage == "DUPLICATE":
        doc = json.load(open(f, encoding="utf-8"))
        doc["agents"].append(dict(doc["agents"][0], id="twin"))
        open(f, "w", encoding="utf-8").write(json.dumps(doc))
    else:
        open(f, "w", encoding="utf-8").write(damage)
    assert store.verify(token) is None


def test_one_bad_entry_is_ignored_and_never_grants_anything(tmp_path):
    f = str(tmp_path / "agents.json")
    good = AT.issue(f, "good", ["projects.read"], days=1)
    doc = json.load(open(f, encoding="utf-8"))
    doc["agents"].append({"id": "greedy", "token_sha256": AT.digest("aga_greedy"), "grants": ["projects.read", "projects.write"], "expires_at": 4_000_000_000})
    doc["agents"].append({"id": "forever", "token_sha256": AT.digest("aga_forever"), "grants": ["projects.read"]})
    open(f, "w", encoding="utf-8").write(json.dumps(doc))
    store = AT.AgentTokenStore(f)
    assert store.verify(good) is not None and store.verify("aga_greedy") is None and store.verify("aga_forever") is None


def test_each_agent_sees_and_may_call_only_what_it_was_granted_and_the_audit_names_it(env, tmp_path):
    f, audit = str(tmp_path / "agents.json"), tmp_path / "audit.jsonl"
    one = AT.issue(f, "projects-only", ["projects.read"], days=1)
    two = AT.issue(f, "missions-only", ["missions.read"], days=1)
    env.setenv("AGENT_GATEWAY_AGENTS_FILE", f)
    env.setenv("AGENT_GATEWAY_AUDIT_PATH", str(audit))
    c = TestClient(create_app(SampleProjectionProvider()))
    a, b = _names(c.get("/api/agent-gateway/capabilities", headers=bearer(one))), _names(c.get("/api/agent-gateway/capabilities", headers=bearer(two)))
    assert a and b and all(n.startswith("projects.") for n in a) and all(n.startswith("missions.") for n in b)
    ok = c.post(f"/api/agent-gateway/capabilities/{a[0]}/invoke", json={}, headers=bearer(one))
    denied = c.post(f"/api/agent-gateway/capabilities/{a[0]}/invoke", json={}, headers=bearer(two))
    assert ok.status_code == 200 and ok.json()["status"] == "ok", ok.text[:200]
    assert denied.json().get("status") != "ok" and not denied.json().get("output"), denied.text[:200]      # not its grant: nothing comes back
    log = audit.read_text(encoding="utf-8")
    assert "agent:projects-only" in log and "agent:missions-only" in log and one not in log and two not in log
    assert c.get("/api/agent-gateway/capabilities", headers=bearer("aga_nope")).status_code == 401


def test_without_a_file_or_a_shared_bearer_nothing_is_mounted(env):
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.get("/api/agent-gateway/capabilities", headers=bearer("x")).status_code == 404


def _gated(env, tmp_path, base=""):
    f = str(tmp_path / "agents.json")
    token = AT.issue(f, "laptop", ["projects.read"], days=1)
    env.setenv("AGENT_GATEWAY_AGENTS_FILE", f)
    env.setenv("AGENT_GATEWAY_TOKEN", "shared-demo-bearer")
    env.setenv("PROJECTS_OPERATOR_GATE", "1")
    env.setenv("OPERATOR_BASIC_AUTH", "ops:pw-123456")
    return f, token


def test_behind_the_sign_in_gate_an_agents_own_token_opens_the_gateway_and_nothing_else(env, tmp_path):
    f, token = _gated(env, tmp_path)
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.get("/api/agent-gateway/capabilities", headers=bearer(token)).status_code == 200
    for path in ("/api/projects", "/api/dashboards", "/", "/index.html", "/api/projects/customer-ops/today",
                 "/api/agent-gateway/../projects", "/api/agent-gateway/%2e%2e/projects", "/api/agent-gatewayx"):
        r = c.get(path, headers=bearer(token))
        assert r.status_code == 401 and "secret ui" not in r.text, (path, r.status_code)
    assert c.post("/api/sidekick", json={"ctx": {}, "text": "hi"}, headers=bearer(token)).status_code == 401
    # the SHARED bearer does not pass the gate: it names no one, and this deployment's data is real
    assert c.get("/api/agent-gateway/capabilities", headers=bearer("shared-demo-bearer")).status_code == 401
    assert c.get("/api/agent-gateway/capabilities").status_code == 401
    AT.revoke(f, "laptop")
    assert c.get("/api/agent-gateway/capabilities", headers=bearer(token)).status_code == 401
    operator = {"Authorization": "Basic " + base64.b64encode(b"ops:pw-123456").decode()}
    assert c.get("/api/projects", headers=operator).status_code == 200                              # the operator is unaffected


def test_a_standard_mcp_client_connects_to_the_projects_api_with_its_own_token(env, tmp_path):
    """The real protocol over a real socket: a uvicorn server running the whole Projects API, gate on, and FastMCP's client."""
    pytest.importorskip("fastmcp", reason="the optional [mcp] extra (fastmcp) is not installed")
    pytest.importorskip("uvicorn", reason="uvicorn is not installed")
    import asyncio
    import socket
    import threading
    import time

    import uvicorn
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    f, token = _gated(env, tmp_path)
    env.setenv("AGENT_GATEWAY_MCP", "1")
    app = create_app(SampleProjectionProvider())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "the server did not start"
        url = f"http://127.0.0.1:{port}/api/agent-gateway/mcp-server/"

        async def talk(t):
            async with Client(StreamableHttpTransport(url, headers=bearer(t))) as client:
                tools = await client.list_tools()
                names = sorted(x.name for x in tools)
                result = await client.call_tool(names[0], {"arguments": {}})
                said = result.data if isinstance(result.data, dict) else result.structured_content
                if not isinstance(said, dict):
                    said = json.loads(result.content[0].text)
                return names, said.get("result", said)

        names, said = asyncio.run(talk(token))
        assert names and all(n.startswith("projects.") for n in names), names
        assert said["status"] == "ok" and said["output"], str(said)[:300]
        for bad in ("aga_nope", "shared-demo-bearer"):
            with pytest.raises(Exception):
                asyncio.run(talk(bad))
        AT.revoke(f, "laptop")
        with pytest.raises(Exception):
            asyncio.run(talk(token))
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_asking_for_the_mcp_server_without_the_extra_stops_startup(env, tmp_path, monkeypatch):
    env.setenv("AGENT_GATEWAY_TOKEN", "t")
    env.setenv("AGENT_GATEWAY_MCP", "1")
    import agentic_os.agent_gateway as AG
    monkeypatch.setattr(AG, "build_fastmcp_server", lambda *a, **k: (_ for _ in ()).throw(ImportError("no fastmcp")))
    with pytest.raises(RuntimeError, match="pip install"):
        create_app(SampleProjectionProvider())


def test_the_mounted_surface_registers_reads_only(env, tmp_path):
    """The route layer treats the gateway's POSTs as queries. That stays true only while nothing here can write."""
    from agentic_os.agent_gateway.contracts import CapabilityKind
    env.setenv("AGENT_GATEWAY_TOKEN", "t")
    app = create_app(SampleProjectionProvider())
    caps = list(app.state.agent_gateway_registry.all())
    assert caps and all(not getattr(m, "side_effecting", False) for m in caps)
    assert all(all(p in AT.READ_GRANTS for p in m.permissions) and m.permissions for m in caps), [(m.name, m.permissions) for m in caps]
    assert {k for k in app.state.route_policies if "agent-gateway" in k} == {"POST /api/agent-gateway/capabilities/{name}/invoke", "POST /api/agent-gateway/mcp"}


def test_over_a_real_socket_a_capability_can_actually_be_invoked(env, tmp_path):
    """The in-process test client is treated as advisory by the route layer, so only a real request shows whether the POSTs
    work. They answered 403 'no authorization policy' on every real deployment."""
    pytest.importorskip("uvicorn", reason="uvicorn is not installed")
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    env.setenv("AGENT_GATEWAY_TOKEN", "shared")
    app = create_app(SampleProjectionProvider())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}/api/agent-gateway"
        r = httpx.post(f"{base}/capabilities/projects.list/invoke", headers=bearer("shared"), json={})
        assert r.status_code == 200, r.text[:200]
        r = httpx.post(f"{base}/mcp", headers=bearer("shared"), json={"method": "tools/list"})
        assert r.status_code == 200 and r.json()["tools"], r.text[:200]
        assert httpx.post(f"{base}/mcp", headers=bearer("wrong"), json={"method": "tools/list"}).status_code == 401
        assert httpx.post(f"{base}/mcp", json={"method": "tools/list"}).status_code == 401
        # a write method on anything else under the gateway is still refused by the route layer
        assert httpx.post(f"{base}/capabilities", headers=bearer("shared"), json={}).status_code in (403, 405)
        assert httpx.delete(f"{base}/mcp", headers=bearer("shared")).status_code in (403, 405)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ── temporary identities: a lifetime in minutes and a rate limit ─────────────────────────────────
def test_a_lifetime_in_minutes_ends_when_it_says(tmp_path):
    f, now = str(tmp_path / "agents.json"), [1_000_000.0]
    store = AT.AgentTokenStore(f, clock=lambda: now[0])
    token = AT.issue(f, "visitor", ["projects.read"], minutes=60, now=now[0])
    now[0] += 3599
    assert store.verify(token) is not None
    now[0] += 1
    assert store.verify(token) is None


@pytest.mark.parametrize("bad", [dict(), dict(days=1, minutes=5), dict(minutes=0), dict(minutes=1441), dict(minutes=5, rate_per_minute=0),
                                 dict(minutes=5, rate_per_minute=601)])
def test_a_lifetime_or_rate_out_of_range_issues_nothing(tmp_path, bad):
    f = tmp_path / "agents.json"
    with pytest.raises(ValueError):
        AT.issue(str(f), "visitor", ["projects.read"], **bad)
    assert not f.exists()


@pytest.mark.parametrize("bad", [0, -1, 601, "30", True, 1.5, None])
def test_an_entry_with_an_unreadable_rate_signs_nobody_in(tmp_path, bad):
    f = str(tmp_path / "agents.json")
    token = AT.issue(f, "visitor", ["projects.read"], minutes=5, rate_per_minute=5)
    doc = json.load(open(f))
    doc["agents"][0]["rate_per_minute"] = bad
    json.dump(doc, open(f, "w"))
    assert AT.AgentTokenStore(f).verify(token) is None


def test_each_agent_is_held_to_its_own_rate_over_any_sixty_seconds(tmp_path):
    f, tick = str(tmp_path / "agents.json"), [0.0]
    store = AT.AgentTokenStore(f)
    tokens = {n: AT.issue(f, n, ["projects.read"], minutes=5, **kw) for n, kw in (("b", {"rate_per_minute": 2}), ("c", {"rate_per_minute": 1}), ("free", {}))}
    who = {n: store.verify(t) for n, t in tokens.items()}
    limit = AT.AgentRateLimit(store, clock=lambda: tick[0])
    assert [limit.check(who["b"], None)[0] for _ in range(3)] == [True, True, False]
    assert limit.check(who["c"], None)[0] and not limit.check(who["c"], None)[0]          # its own window
    assert all(limit.check(who["free"], None)[0] for _ in range(50))                      # no limit set
    tick[0] = 59.9
    assert not limit.check(who["b"], None)[0]
    tick[0] = 60.0
    assert limit.check(who["b"], None)[0], "refused calls must not have extended the window"


def test_over_the_mounted_gateway_the_limit_refuses_the_call(env, tmp_path):
    f = str(tmp_path / "agents.json")
    token = AT.issue(f, "visitor", ["projects.read"], minutes=5, rate_per_minute=2)
    env.setenv("AGENT_GATEWAY_AGENTS_FILE", f)
    client = TestClient(create_app(SampleProjectionProvider()))
    name = _names(client.get("/api/agent-gateway/capabilities", headers=bearer(token)))[0]
    replies = [client.post(f"/api/agent-gateway/capabilities/{name}/invoke", json={}, headers=bearer(token)) for _ in range(3)]
    statuses = [r.json()["status"] for r in replies]
    assert "denied" not in statuses[:2] and statuses[2] == "denied", statuses        # the caller is not told why
