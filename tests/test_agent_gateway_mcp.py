"""Governed MCP read path — Phase 1 (plan §13 Phase-1 acceptance).

An external MCP client connects with a token, sees only the capabilities it is permitted, cannot
enumerate hidden ones, and every call audits — with no network and no MCP server library (the
bridge is pure). Backends are in-test fakes behind the read-port seams.
"""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway import (
    AgentGateway, DevTokenVerifier, GatewayAuthError, InMemoryAuditSink, McpGatewayBridge,
    build_read_registry)


# ── fake read backends ──────────────────────────────────────────────────────────────
class _Projections:
    def projects(self): return [{"id": "customer-ops", "name": "Customer Operations"}]
    def overview(self, project_id): return {"project_id": project_id, "needs_you": 3}


class _Missions:
    def list(self): return [{"id": "m1", "state": "running"}]
    def get(self, mission_id): return {"id": mission_id, "state": "running"}
    def explain(self, mission_id): return {"id": mission_id, "plan": ["step1", "step2"]}
    def pending_approvals(self): return [{"mission_id": "m1", "node": "refund"}]


class _Crm:
    def lookup(self, query): return {"query": query, "account": "ACME", "email": "a@acme.com"}


def _setup(grants, scopes=(), tenant="acme"):
    """A gateway with all read backends wired + a dev token for a principal with `grants`."""
    dev = DevTokenVerifier()
    registry = build_read_registry(projections=_Projections(), missions=_Missions(), crm=_Crm())
    # note: no `sources` backend → sources.search is NOT registered at all
    gw = AgentGateway(registry=registry, authorize=dev.authorize, audit=InMemoryAuditSink())
    token = dev.issue("agent-user", tenant=tenant, workspace="acme-ws",
                      client_id="claude-desktop", scopes=tuple(scopes), grants=tuple(grants))
    return dev, gw, McpGatewayBridge(gw, dev), token


# ── auth ──────────────────────────────────────────────────────────────────────────
def test_dev_token_roundtrip_and_deny_unknown():
    dev = DevTokenVerifier()
    tok = dev.issue("alice", tenant="acme", grants=("crm.read",))
    gp = dev.verify(tok)
    assert gp is not None and gp.subject == "alice" and gp.tenant == "acme"
    assert dev.authorize(gp.principal, "crm.read") and not dev.authorize(gp.principal, "crm.write")
    assert dev.verify("gwt_not_a_real_token") is None


def test_unauthenticated_calls_raise_auth_error_not_a_capability_leak():
    _, _, bridge, _ = _setup(grants=("projects.read",))
    with pytest.raises(GatewayAuthError):
        bridge.list_tools("")
    with pytest.raises(GatewayAuthError):
        bridge.call_tool("bogus-token", "projects.list", {})


# ── discovery: only the permitted subset is a tool ──────────────────────────────────
def test_list_tools_shows_only_permitted_capabilities():
    _, _, bridge, token = _setup(grants=("projects.read", "crm.read"))
    names = {t["name"] for t in bridge.list_tools(token)}
    assert names == {"projects.list", "projects.get_status", "crm.lookup_account"}
    # missions.* hidden (no missions.read grant); sources.search absent (no backend registered)
    assert not any(n.startswith("missions.") for n in names)
    assert "sources.search" not in names


def test_tool_descriptors_have_mcp_shape():
    _, _, bridge, token = _setup(grants=("crm.read",))
    tool = next(t for t in bridge.list_tools(token) if t["name"] == "crm.lookup_account")
    assert tool["description"] and tool["inputSchema"]["type"] == "object"
    assert "query" in tool["inputSchema"]["properties"]


# ── the governed read call ──────────────────────────────────────────────────────────
def test_call_read_tool_returns_output_and_audits():
    _, gw, bridge, token = _setup(grants=("missions.read",))
    out = bridge.call_tool(token, "missions.explain", {"mission_id": "m1"})
    assert out["status"] == "ok" and out["output"] == {"id": "m1", "plan": ["step1", "step2"]}
    assert gw.audit.events[-1].capability == "missions.explain"
    assert gw.audit.events[-1].tenant == "acme"                 # tenant carried onto the audit record


def test_call_hidden_tool_denies_without_leaking():
    _, gw, bridge, token = _setup(grants=("projects.read",))   # no crm.read
    out = bridge.call_tool(token, "crm.lookup_account", {"query": "ACME"})
    assert out == {"status": "denied", "request_id": out["request_id"]}   # no output, no reason
    # and a genuinely missing capability looks identical
    missing = bridge.call_tool(token, "no.such.tool", {})
    assert missing["status"] == "denied"


def test_call_missing_argument_is_handled_not_crashed():
    _, _, bridge, token = _setup(grants=("projects.read",))
    out = bridge.call_tool(token, "projects.get_status", {})   # project_id omitted
    assert out["status"] == "ok" and out["output"]["project_id"] == ""   # backend saw empty id


def test_build_read_registry_registers_only_provided_backends():
    reg = build_read_registry(projections=_Projections())      # missions/sources/crm omitted
    names = {m.name for m in reg.all()}
    assert names == {"projects.list", "projects.get_status"}


def test_every_mcp_call_including_denials_audits_once():
    _, gw, bridge, token = _setup(grants=("projects.read",))
    bridge.call_tool(token, "projects.list", {})
    bridge.call_tool(token, "crm.lookup_account", {"query": "x"})   # denied (no grant)
    assert len(gw.audit.events) == 2
    assert [e.status.value for e in gw.audit.events] == ["ok", "denied"]
