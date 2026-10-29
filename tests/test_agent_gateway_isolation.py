"""Isolation properties of the Agent Gateway that the per-feature suites do not pin: redirect-URI binding in the OAuth flow, tenant separation of approvals and idempotent results, and the list/call parity of permission filtering. Synthetic data, no network."""
from __future__ import annotations

import pytest

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, AuthorizationServer, CapabilityRegistry, DevTokenVerifier, HandlerResult, InboxApprovalStore,
    McpGatewayBridge, MCPEndpoint)
from agentic_os.agent_gateway.auth import GatewayAuthError
from agentic_os.agent_gateway.contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, RiskTier
from agentic_os.agent_gateway.oauth import OAuthError, pkce_challenge

VERIFIER = "v" * 50


# ── OAuth: the redirect URI is bound to the registration and to the code ────────────
def _client(srv):
    return srv.register_client(["https://app.example/cb"])


def test_authorize_refuses_an_unregistered_redirect_uri():
    srv = AuthorizationServer()
    client = _client(srv)
    with pytest.raises(OAuthError) as e:
        srv.authorize(client_id=client.client_id, redirect_uri="https://evil.example/cb",
                      code_challenge=pkce_challenge(VERIFIER), principal=Principal("dana", "service", (), "t1"))
    assert e.value.error == "invalid_request"


def test_a_code_redeemed_with_a_different_redirect_uri_is_refused_and_burned():
    srv = AuthorizationServer()
    client = _client(srv)
    code = srv.authorize(client_id=client.client_id, redirect_uri="https://app.example/cb",
                         code_challenge=pkce_challenge(VERIFIER), principal=Principal("dana", "service", (), "t1"))
    with pytest.raises(OAuthError) as e:
        srv.exchange(code=code, code_verifier=VERIFIER, client_id=client.client_id, redirect_uri="https://app.example/other")
    assert e.value.error == "invalid_grant"
    with pytest.raises(OAuthError):                                    # the same code, now with the right URI, is gone
        srv.exchange(code=code, code_verifier=VERIFIER, client_id=client.client_id, redirect_uri="https://app.example/cb")


def test_a_token_carries_the_consenting_principals_tenant_and_scopes():
    srv = AuthorizationServer()
    client = _client(srv)
    code = srv.authorize(client_id=client.client_id, redirect_uri="https://app.example/cb",
                         code_challenge=pkce_challenge(VERIFIER), principal=Principal("dana", "service", (), "t1"), scope=("data.read",))
    tok = srv.exchange(code=code, code_verifier=VERIFIER, client_id=client.client_id, redirect_uri="https://app.example/cb")
    gp = srv.verify(tok.access_token)
    assert gp is not None and gp.tenant == "t1" and gp.scopes == ("data.read",)


# ── tenants: approvals and idempotent results are not shared ────────────────────────
def _write_setup():
    reg, writes = CapabilityRegistry(), []

    def writer(req, env):
        writes.append(req.principal.tenant)
        return HandlerResult(ok=True, output={"done": True}, data_classes=(DataClass.INTERNAL,))

    reg.register(CapabilityManifest("notes.write", "writes a note", CapabilityKind.DIRECT, permissions=("notes.write",),
                                    risk_tier=RiskTier.WRITE_LOW if hasattr(RiskTier, "WRITE_LOW") else list(RiskTier)[1],
                                    side_effecting=True, idempotent=True, provider="notes",
                                    approval_policy=ApprovalPolicy.REQUIRED, data_classes=(DataClass.INTERNAL,)), writer)
    verifier, store = DevTokenVerifier(), InboxApprovalStore()
    ta = verifier.issue("carol", tenant="tA", grants=("notes.write",))
    tb = verifier.issue("carol", tenant="tB", grants=("notes.write",))
    bridge = McpGatewayBridge(AgentGateway(registry=reg, authorize=verifier.authorize, approvals=store), verifier)
    return bridge, store, ta, tb, writes


ARGS = {"text": "hello", "idempotency_key": "w1"}


def test_one_tenants_approval_does_not_satisfy_the_same_subjects_identical_request_in_another_tenant():
    bridge, store, ta, tb, writes = _write_setup()
    assert bridge.call_tool(ta, "notes.write", dict(ARGS))["status"] == "pending_approval"
    store.approve(store.pending()[0]["id"], by="manager")              # tenant A's request only
    assert bridge.call_tool(tb, "notes.write", dict(ARGS))["status"] == "pending_approval"
    assert writes == []
    assert bridge.call_tool(ta, "notes.write", dict(ARGS))["status"] == "ok" and writes == ["tA"]


def test_a_stored_idempotent_result_is_not_replayed_to_another_tenant():
    bridge, store, ta, tb, writes = _write_setup()
    bridge.call_tool(ta, "notes.write", dict(ARGS))
    store.approve(store.pending()[0]["id"], by="manager")
    assert bridge.call_tool(ta, "notes.write", dict(ARGS))["status"] == "ok"
    assert bridge.call_tool(tb, "notes.write", dict(ARGS))["status"] == "pending_approval"


def test_the_approval_inbox_shows_who_and_what_but_not_the_arguments():
    bridge, store, ta, _, _ = _write_setup()
    bridge.call_tool(ta, "notes.write", dict(ARGS))
    item = store.pending()[0]
    assert item["tenant"] == "tA" and item["capability"] == "notes.write"
    assert "arguments" not in item and "hello" not in str(item)


def test_a_requester_cannot_approve_their_own_request():
    bridge, store, ta, _, _ = _write_setup()
    bridge.call_tool(ta, "notes.write", dict(ARGS))
    with pytest.raises(Exception):
        store.approve(store.pending()[0]["id"], by="carol")


# ── listing and calling agree about what a caller may see ───────────────────────────
def _read(name, *, scopes=(), perms=()):
    return CapabilityManifest(name, "reads synthetic data", CapabilityKind.DIRECT, permissions=perms, scopes=scopes,
                              data_classes=(DataClass.INTERNAL,))


def test_list_is_filtered_by_scope_and_permission_and_a_hidden_tool_looks_unknown():
    reg = CapabilityRegistry()
    ok = lambda r, e: HandlerResult(ok=True, output={"x": 1}, data_classes=(DataClass.INTERNAL,))
    reg.register(_read("scoped.read", scopes=("data.read",), perms=("data.read",)), ok)
    reg.register(_read("perm.read", perms=("secret.read",)), ok)
    verifier = DevTokenVerifier()
    no_scope = verifier.issue("erin", tenant="t1", scopes=(), grants=("data.read", "secret.read"))
    scoped = verifier.issue("fay", tenant="t1", scopes=("data.read",), grants=("data.read",))
    bridge = McpGatewayBridge(AgentGateway(registry=reg, authorize=verifier.authorize), verifier)
    names = lambda t: sorted(x["name"] for x in bridge.list_tools(t))
    assert names(no_scope) == ["perm.read"] and names(scoped) == ["scoped.read"]
    hidden, unknown = bridge.call_tool(no_scope, "scoped.read", {}), bridge.call_tool(no_scope, "does.not.exist", {})
    assert hidden["status"] == unknown["status"] == "denied"
    assert set(hidden) == set(unknown) == {"status", "request_id"}


@pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", None])
def test_a_malformed_authorization_header_is_refused(header):
    verifier = DevTokenVerifier()
    endpoint = MCPEndpoint(McpGatewayBridge(AgentGateway(registry=CapabilityRegistry(), authorize=verifier.authorize), verifier))
    with pytest.raises(GatewayAuthError):
        endpoint.handle(header, "tools/list")


def test_an_unknown_mcp_method_is_a_structured_error():
    verifier = DevTokenVerifier()
    tok = verifier.issue("erin", tenant="t1", grants=())
    endpoint = MCPEndpoint(McpGatewayBridge(AgentGateway(registry=CapabilityRegistry(), authorize=verifier.authorize), verifier))
    assert endpoint.handle("Bearer " + tok, "nope/x") == {"error": {"code": "method_not_found", "message": "unknown method nope/x"}}
