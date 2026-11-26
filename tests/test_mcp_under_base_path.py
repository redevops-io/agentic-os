"""The real MCP server answers an MCP client whether or not Projects is served under a base path. A mounted application's
own lifespan is never run by its parent, so under PROJECTS_BASE_PATH the MCP session manager was never started and every
client failed at ``initialize`` (found by the official SDK against a deployment, 2026-11-26)."""
import pytest

pytest.importorskip("fastmcp")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.projects_api import mounted_app  # noqa: E402

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


@pytest.mark.parametrize("base", ["", "/projects"])
def test_an_mcp_client_can_initialize_and_list_tools(base, monkeypatch, tmp_path):
    for k in ("AGENT_GATEWAY_TOKEN", "AGENTIC_OS_OAUTH_DB", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_TENANT_LIMITS", "AGENTIC_OS_PROFILE",
              "PROJECTS_OPERATOR_GATE", "OPERATOR_BASIC_AUTH", "OUTREACH_BASIC_AUTH", "AGENTIC_OS_WRITES_DIR"):
        monkeypatch.delenv(k, raising=False)
    agents = str(tmp_path / "agents.json")
    monkeypatch.setenv("AGENT_GATEWAY_AGENTS_FILE", agents)
    monkeypatch.setenv("AGENT_GATEWAY_MCP", "1")
    token = AT.issue(agents, "mcp-1", ["projects.read"], minutes=5)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    url = f"{base}/api/agent-gateway/mcp-server/"
    with TestClient(mounted_app(base), base_url="http://localhost", raise_server_exceptions=False) as client:   # runs the lifespan, as a server does
        first = client.post(url, headers=headers, json=INITIALIZE)
        assert first.status_code == 200, (first.status_code, first.text[:300])
        assert "serverInfo" in first.text
        session = first.headers.get("mcp-session-id")
        more = {**headers, **({"mcp-session-id": session} if session else {})}
        client.post(url, headers=more, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = client.post(url, headers=more, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listed.status_code == 200 and "projects.list" in listed.text, (listed.status_code, listed.text[:300])
