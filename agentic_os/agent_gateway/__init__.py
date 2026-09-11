"""ReDevOps Governed Agent Gateway — the governed northbound path for external agents.

Bring any agent (Claude, ChatGPT, Cursor, custom). ReDevOps governs what it can do inside your
systems: every operation crosses one path — identity → permissions → risk → budget → approval →
GovernedEnvelope → invoke (direct capability or Mission delegation) → egress policy → audit.

Phase 0 (this module): the contracts, the policy-scoped capability registry, and the single
governed invocation pipeline — protocol-agnostic and fully testable in-process. Protocol adapters
(MCP over Streamable-HTTP with OAuth 2.1 + PKCE, then REST) plug in above :meth:`AgentGateway.invoke`
in later phases; they add no new authority. See GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md.
"""
from .contracts import (
    CONTRACT_VERSION, ApprovalPolicy, AuditEvent, CapabilityKind, CapabilityManifest, DataClass,
    EgressAction, GatewayDecision, GatewayPrincipal, GatewayRequest, GatewayResult, GatewayStatus,
    RiskTier)
from .gateway import (
    AgentGateway, ApprovalStore, AuditSink, BudgetGuard, EgressPolicy, InMemoryAuditSink,
    InMemoryIdempotencyStore, IdempotencyStore, MissionDelegation, MissionPort)
from .registry import CapabilityHandler, CapabilityRegistry, HandlerResult
from .auth import DevTokenVerifier, GatewayAuthError, TokenVerifier, bearer_token
from .capabilities import READ_CAPABILITIES, build_read_registry, register_mission_capabilities
from .mission_adapter import MissionRuntimeAdapter
from .write_capabilities import WRITE_CAPABILITIES, build_write_registry
from .protocols import McpGatewayBridge, mcp_tool_descriptors

__all__ = [
    "CONTRACT_VERSION",
    # contracts
    "RiskTier", "ApprovalPolicy", "EgressAction", "DataClass", "CapabilityKind",
    "CapabilityManifest", "GatewayPrincipal", "GatewayRequest", "GatewayDecision",
    "GatewayResult", "GatewayStatus", "AuditEvent",
    # registry
    "CapabilityRegistry", "CapabilityHandler", "HandlerResult",
    # gateway + seams
    "AgentGateway", "MissionPort", "MissionDelegation", "EgressPolicy", "ApprovalStore",
    "BudgetGuard", "AuditSink", "IdempotencyStore", "InMemoryAuditSink", "InMemoryIdempotencyStore",
    # auth
    "TokenVerifier", "DevTokenVerifier", "GatewayAuthError", "bearer_token",
    # read capabilities + MCP adapter
    "build_read_registry", "READ_CAPABILITIES", "McpGatewayBridge", "mcp_tool_descriptors",
    # mission delegation (Phase 2)
    "MissionRuntimeAdapter", "register_mission_capabilities",
    # governed write capabilities (Phase 3)
    "build_write_registry", "WRITE_CAPABILITIES",
]
