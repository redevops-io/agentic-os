"""FastMCP Streamable-HTTP transport binding — the optional [mcp] extra.

Skipped when fastmcp isn't installed. The dispatch + governance semantics are the (already tested)
McpGatewayBridge; here we verify the FastMCP server BUILDS and registers the gateway's capabilities
as tools. End-to-end auth/list-filtering is exercised over HTTP in deployment (the in-memory
transport can't carry a bearer token).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastmcp")

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import AgentGateway, DevTokenVerifier, build_fastmcp_server
from agentic_os.agent_gateway.capabilities import build_read_registry


class _Missions:
    def list(self): return []
    def get(self, mid): return {}
    def explain(self, mid): return {}
    def pending_approvals(self): return []


def _server():
    dev = DevTokenVerifier()
    gw = AgentGateway(registry=build_read_registry(missions=_Missions()), authorize=dev.authorize)
    return build_fastmcp_server(gw, dev, name="test-gateway")


def test_fastmcp_server_builds_and_registers_gateway_capabilities():
    server = _server()
    # get_tool hits the registry directly (list_tools would apply the principal filter middleware,
    # which returns nothing without an authenticated request).
    for name in ("missions.list", "missions.get", "missions.explain",
                 "missions.list_pending_approvals"):
        tool = asyncio.run(server.get_tool(name))
        assert tool is not None and tool.name == name    # the read caps became MCP tools


def test_fastmcp_server_has_an_auth_verifier_wired():
    server = _server()
    assert server.auth is not None                        # every request is authenticated
