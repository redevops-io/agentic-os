"""The Governed Agent Gateway mounted on the served Projects app (read-only, opt-in).

Exercises the live HTTP surface an external agent uses (REST + MCP), backed by the real projection
provider, gated by AGENT_GATEWAY_TOKEN, with egress enforcement on the way out.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app

TOKEN = "agw-demo-secret-DO-NOT-STORE"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", TOKEN)
    return TestClient(create_app(SampleProjectionProvider()))


def test_gateway_is_absent_unless_a_token_is_configured(monkeypatch):
    monkeypatch.delenv("AGENT_GATEWAY_TOKEN", raising=False)
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.get("/api/agent-gateway/capabilities", headers=AUTH).status_code == 404  # not mounted


def test_capabilities_list_is_read_only_and_requires_the_bearer(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/agent-gateway/capabilities").status_code == 401          # no token
    assert c.get("/api/agent-gateway/capabilities",
                 headers={"Authorization": "Bearer wrong"}).status_code == 401  # bad token
    caps = c.get("/api/agent-gateway/capabilities", headers=AUTH).json()["capabilities"]
    names = {x["name"] for x in caps}
    assert {"projects.list", "projects.get_status", "missions.list", "sources.search"} <= names
    assert not any(n.startswith("crm.request") or n == "sandbox.execute" for n in names)  # no writes


def test_rest_invoke_runs_through_the_governed_path(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=AUTH, json={})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert isinstance(r.json()["output"], list) and r.json()["output"]


def test_mcp_endpoint_lists_and_calls_tools(monkeypatch):
    c = _client(monkeypatch)
    listed = c.post("/api/agent-gateway/mcp", headers=AUTH, json={"method": "tools/list"}).json()
    assert "projects.list" in {t["name"] for t in listed["tools"]}
    called = c.post("/api/agent-gateway/mcp", headers=AUTH,
                    json={"method": "tools/call", "params": {"name": "projects.list"}}).json()
    assert called["status"] == "ok"
    # unauthenticated MCP call → 401
    assert c.post("/api/agent-gateway/mcp", json={"method": "tools/list"}).status_code == 401


def test_a_write_capability_is_not_reachable(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/agent-gateway/capabilities/crm.request_outreach_send/invoke",
               headers=AUTH, json={"contact": "x"})
    # not registered on this read-only surface ⇒ denied as unknown (no leak)
    assert r.status_code == 200 and r.json()["status"] == "denied"
