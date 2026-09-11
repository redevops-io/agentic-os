"""MCP adapter over the Governed Agent Gateway (plan §10).

The bridge is pure and transport-independent — it maps MCP's two operations onto the gateway:

    list_tools  → AgentGateway.capabilities_for(principal)   (the *filtered* set only)
    call_tool   → AgentGateway.invoke(...).client_view()     (the one governed path)

so it is fully testable without an MCP server library. :func:`build_fastmcp_server` is a thin,
optional shell that wires a real FastMCP Streamable-HTTP server to a bridge; it imports ``fastmcp``
lazily so the core package needs no such dependency. The bridge never invents authority: a token
that doesn't verify raises :class:`GatewayAuthError`, and a capability the principal can't see is
neither listed nor callable (it denies exactly as a missing one would — no leak, plan §18).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..auth import GatewayAuthError, TokenVerifier
from ..contracts import GatewayRequest
from ..gateway import AgentGateway


def mcp_tool_descriptors(public_views: List[dict]) -> List[dict]:
    """Turn gateway capability public-views into MCP tool descriptors (name/description/inputSchema)."""
    return [{"name": v["name"], "description": v["description"],
             "inputSchema": v.get("input_schema") or {"type": "object", "properties": {}}}
            for v in public_views]


class McpGatewayBridge:
    """Serves MCP ``tools/list`` and ``tools/call`` for one gateway, authenticating each call."""

    def __init__(self, gateway: AgentGateway, verifier: TokenVerifier) -> None:
        self.gateway = gateway
        self.verifier = verifier

    def _principal(self, token: str):
        gp = self.verifier.verify(token or "")
        if gp is None:
            raise GatewayAuthError("invalid or missing token")
        return gp

    def list_tools(self, token: str) -> List[dict]:
        """MCP tools/list — only the capabilities this principal is permitted to see."""
        gp = self._principal(token)
        return mcp_tool_descriptors(self.gateway.capabilities_for(gp))

    def call_tool(self, token: str, name: str, arguments: Dict[str, Any] | None = None) -> dict:
        """MCP tools/call — one governed invocation; returns the client-safe view of the result."""
        gp = self._principal(token)
        result = self.gateway.invoke(
            GatewayRequest(gp, name, arguments or {}, protocol="mcp",
                           idempotency_key=(arguments or {}).get("idempotency_key")))
        return result.client_view()


def build_fastmcp_server(bridge: McpGatewayBridge, *, name: str = "redevops-agent-gateway"):
    """Optional: a FastMCP Streamable-HTTP server wired to the bridge. Imports ``fastmcp`` lazily —
    call this only where that dependency is installed (it is not a core requirement). The server's
    auth middleware must place the verified bearer token where ``token_getter`` can read it."""
    from fastmcp import FastMCP  # noqa: F401  (lazy; optional dependency)

    raise NotImplementedError(
        "wire FastMCP's Streamable-HTTP transport + OAuth resource-server auth to bridge.list_tools/"
        "call_tool in the deployment shell; Phase 1b provides the OAuth 2.1 + PKCE server. The pure "
        "bridge above is the tested contract.")
