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

from typing import Optional

from ..auth import GatewayAuthError, TokenVerifier, bearer_token
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


class MCPEndpoint:
    """Transport-agnostic MCP resource-server boundary. A transport (Streamable-HTTP, stdio, an ASGI
    handler) hands it the request's Authorization header + the MCP method/params; it extracts the
    bearer token, and dispatches ``tools/list`` / ``tools/call`` through the bridge. This keeps the
    OAuth-resource-server + governance boundary testable independently of any MCP server library."""

    def __init__(self, bridge: McpGatewayBridge) -> None:
        self.bridge = bridge

    def handle(self, authorization: Optional[str], method: str, params: Optional[dict] = None) -> dict:
        token = bearer_token(authorization)
        if not token:
            raise GatewayAuthError("missing bearer token")     # transport → 401
        params = params or {}
        if method == "tools/list":
            return {"tools": self.bridge.list_tools(token)}
        if method == "tools/call":
            name = params.get("name")
            if not name:
                return {"error": {"code": "invalid_params", "message": "tool name required"}}
            return self.bridge.call_tool(token, name, params.get("arguments") or {})
        return {"error": {"code": "method_not_found", "message": f"unknown method {method}"}}


def build_fastmcp_server(gateway: AgentGateway, verifier: TokenVerifier, *,
                         name: str = "redevops-agent-gateway"):
    """A FastMCP Streamable-HTTP server over the gateway (plan §10). Optional — imports ``fastmcp``
    lazily (install the ``mcp`` extra); the core package needs no such dependency.

    Wiring: a FastMCP ``TokenVerifier`` wraps our :class:`TokenVerifier` (bearer → GatewayPrincipal),
    so every request is authenticated; each registered capability is exposed as a tool that routes
    through the gateway's one governed path; and a middleware filters ``tools/list`` to the
    principal-visible set so a hidden capability is never enumerated. Serve it with
    ``server.run(transport="http")`` (Streamable-HTTP) or ``run_async``.

    Each tool publishes the capability's own input schema, so a client that has never heard of this gateway knows what to
    send, and says whether it only reads. A call runs in a worker thread: a slow capability does not stall the server.
    (A call shaped ``{"arguments": {...}}``, the first release's only form, is still accepted for a capability that has no
    input of that name.) The dispatch + governance semantics are those of the (tested) :class:`McpGatewayBridge`.
    """
    import functools
    import json

    from anyio import to_thread
    from fastmcp import FastMCP
    from fastmcp.server.auth import AccessToken, TokenVerifier as _FastMCPTokenVerifier
    from fastmcp.server.dependencies import get_access_token
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools import Tool, ToolResult
    from mcp_types import ToolAnnotations

    bridge = McpGatewayBridge(gateway, verifier)

    class _Verifier(_FastMCPTokenVerifier):
        async def verify_token(self, token: str):
            gp = verifier.verify(token)
            if gp is None:
                return None
            return AccessToken(token=token, client_id=gp.client_id, scopes=list(gp.scopes),
                               subject=gp.subject,
                               claims={"tenant": gp.tenant, "workspace": gp.effective_workspace})

    # mask_error_details: an exception that escapes a tool function (anything the gateway's own error handling did not catch) reaches the client as
    # "Error calling tool '<name>'" only; its text stays in the server log.
    server = FastMCP(name, auth=_Verifier(), mask_error_details=True)

    class _CapabilityTool(Tool):
        async def run(self, arguments: Dict[str, Any]) -> ToolResult:
            args = dict(arguments or {})
            if set(args) == {"arguments"} and "arguments" not in (self.parameters.get("properties") or {}):
                inner = args["arguments"]
                if inner is not None and not isinstance(inner, dict):
                    return ToolResult(content="invalid arguments: send the capability's inputs as an object", is_error=True)
                args = inner or {}
            at = get_access_token()
            view = await to_thread.run_sync(functools.partial(bridge.call_tool, at.token if at else "", self.name, args))
            view = json.loads(json.dumps(view, default=str))              # what is sent must be plain JSON
            return ToolResult(content=json.dumps(view), structured_content=view)

    for manifest in gateway.registry.all():
        schema = {"type": "object", "properties": {}, **dict(manifest.input_schema or {})}
        server.add_tool(_CapabilityTool(name=manifest.name, description=manifest.description, parameters=schema,
                                        annotations=ToolAnnotations(readOnlyHint=not manifest.side_effecting)))

    class _PrincipalFilter(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            at = get_access_token()
            gp = verifier.verify(at.token) if at else None
            if gp is None:
                return []
            visible = {m.name for m in gateway.registry.visible_for(gp, gateway.authorize)}
            return [t for t in tools if t.name in visible]

    server.add_middleware(_PrincipalFilter())
    return server
