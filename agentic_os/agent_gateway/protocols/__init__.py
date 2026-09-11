"""Protocol adapters for the Governed Agent Gateway.

Each adapter terminates an external protocol and calls :meth:`AgentGateway.invoke` — it adds no
authority of its own. MCP is the first (plan §10); REST and others follow (plan §7 Phase 7).
"""
from .mcp import MCPEndpoint, McpGatewayBridge, build_fastmcp_server, mcp_tool_descriptors
from .rest import RestGatewayAdapter

__all__ = ["McpGatewayBridge", "MCPEndpoint", "mcp_tool_descriptors", "build_fastmcp_server",
           "RestGatewayAdapter"]
