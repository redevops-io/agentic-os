"""OAuth 2.1 + PKCE authorization server + MCP resource-server endpoint — Phase 1b (plan §3.2, §10, §14).

Covers the flow, PKCE, single-use codes, expiry, audience restriction, refresh rotation, and
revocation — then wires the AS as the MCP bridge's TokenVerifier and drives the governed read path
end-to-end over a bearer token (the "OAuth-based remote access" the plan calls for).
"""
from __future__ import annotations

import pytest

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, AuthorizationServer, DevTokenVerifier, GatewayAuthError, MCPEndpoint,
    McpGatewayBridge, OAuthError, build_read_registry, pkce_challenge)


class _Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _server(clock=None, events=None):
    return AuthorizationServer(clock=clock or (lambda: 1_000_000.0),
                              audit=(events.append if events is not None else None))


def _authorized_code(srv, principal, *, scope=(), challenge=None, redirect="https://app/cb",
                     workspace="acme-ws"):
    client = srv.register_client([redirect])                 # DCR, public client
    verifier = "verifier-" + "x" * 50
    code = srv.authorize(client_id=client.client_id, redirect_uri=redirect,
                         code_challenge=challenge or pkce_challenge(verifier), principal=principal,
                         scope=tuple(scope), workspace=workspace)
    return client, verifier, code, redirect


# ── the happy path ──────────────────────────────────────────────────────────────
def test_authorization_code_pkce_flow_yields_a_verifiable_token():
    srv = _server()
    p = Principal("alice", "user", (), "acme")
    client, verifier, code, redirect = _authorized_code(srv, p, scope=("crm",))
    tok = srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id,
                       redirect_uri=redirect)
    assert tok.access_token.startswith("at_") and tok.refresh_token.startswith("rt_")
    gp = srv.verify(tok.access_token)
    assert gp is not None and gp.subject == "alice" and gp.tenant == "acme"
    assert gp.scopes == ("crm",) and gp.effective_workspace == "acme-ws"


def test_pkce_mismatch_is_rejected():
    srv = _server()
    client, _, code, redirect = _authorized_code(srv, Principal("a", "user", (), "acme"))
    with pytest.raises(OAuthError) as e:
        srv.exchange(code=code, code_verifier="the-wrong-verifier", client_id=client.client_id,
                     redirect_uri=redirect)
    assert e.value.error == "invalid_grant"


def test_plain_pkce_method_is_refused():
    srv = _server()
    client = srv.register_client(["https://app/cb"])
    with pytest.raises(OAuthError):
        srv.authorize(client_id=client.client_id, redirect_uri="https://app/cb",
                      code_challenge="x", principal=Principal("a", "user", (), "t"),
                      code_challenge_method="plain")


def test_authorization_code_is_single_use():
    srv = _server()
    client, verifier, code, redirect = _authorized_code(srv, Principal("a", "user", (), "t"))
    srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id, redirect_uri=redirect)
    with pytest.raises(OAuthError) as e:
        srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id,
                     redirect_uri=redirect)
    assert e.value.error == "invalid_grant"


def test_expired_code_and_expired_access_token():
    clk = _Clock()
    srv = _server(clock=clk)
    client, verifier, code, redirect = _authorized_code(srv, Principal("a", "user", (), "t"))
    clk.advance(120)                                          # past code_ttl (60s)
    with pytest.raises(OAuthError):
        srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id, redirect_uri=redirect)
    # a fresh token then expires after access_ttl
    client2, v2, code2, r2 = _authorized_code(srv, Principal("a", "user", (), "t"))
    tok = srv.exchange(code=code2, code_verifier=v2, client_id=client2.client_id, redirect_uri=r2)
    assert srv.verify(tok.access_token) is not None
    clk.advance(1000)                                         # past access_ttl (900s)
    assert srv.verify(tok.access_token) is None


def test_audience_restriction():
    srv = _server()
    client = srv.register_client(["https://app/cb"])
    v = "verifier-" + "y" * 50
    code = srv.authorize(client_id=client.client_id, redirect_uri="https://app/cb",
                         code_challenge=pkce_challenge(v), principal=Principal("a", "user", (), "t"),
                         audience="some-other-resource")
    tok = srv.exchange(code=code, code_verifier=v, client_id=client.client_id,
                       redirect_uri="https://app/cb")
    assert srv.verify(tok.access_token) is None               # default audience ≠ token audience
    assert srv.verify(tok.access_token, audience="some-other-resource") is not None


# ── refresh rotation + revocation ────────────────────────────────────────────────
def test_refresh_rotates_and_old_tokens_die():
    srv = _server()
    client, verifier, code, redirect = _authorized_code(srv, Principal("a", "user", (), "acme"),
                                                        scope=("crm",))
    tok = srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id,
                       redirect_uri=redirect)
    tok2 = srv.refresh(refresh_token=tok.refresh_token, client_id=client.client_id)
    assert tok2.access_token != tok.access_token and tok2.refresh_token != tok.refresh_token
    assert srv.verify(tok2.access_token) is not None
    assert srv.verify(tok.access_token) is None               # old access invalidated on rotation
    with pytest.raises(OAuthError):                           # old refresh cannot be reused
        srv.refresh(refresh_token=tok.refresh_token, client_id=client.client_id)


def test_revocation():
    srv = _server()
    client, verifier, code, redirect = _authorized_code(srv, Principal("a", "user", (), "acme"))
    tok = srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id,
                       redirect_uri=redirect)
    srv.revoke(tok.access_token)
    assert srv.verify(tok.access_token) is None


def test_confidential_client_needs_its_secret():
    srv = _server()
    client = srv.register_client(["https://app/cb"], public=False, client_secret="s3cr3t")
    v = "verifier-" + "z" * 50
    code = srv.authorize(client_id=client.client_id, redirect_uri="https://app/cb",
                         code_challenge=pkce_challenge(v), principal=Principal("a", "user", (), "t"))
    with pytest.raises(OAuthError) as e:
        srv.exchange(code=code, code_verifier=v, client_id=client.client_id,
                     redirect_uri="https://app/cb")           # no secret
    assert e.value.error == "invalid_client"


def test_grants_are_audited():
    events = []
    srv = _server(events=events)
    client, verifier, code, redirect = _authorized_code(srv, Principal("a", "user", (), "t"))
    srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id, redirect_uri=redirect)
    kinds = [e["event"] for e in events]
    assert "client_registered" in kinds and "code_issued" in kinds and "token_issued" in kinds


# ── end to end: OAuth token drives the governed MCP read path ──────────────────────
def _gateway_with_reads():
    class _Missions:
        def list(self): return [{"id": "m1", "state": "running"}]
        def get(self, mid): return {"id": mid}
        def explain(self, mid): return {"id": mid, "plan": ["a"]}
        def pending_approvals(self): return []
    dev_authz = lambda p, perm: perm == "missions.read"       # principal granted missions.read
    reg = build_read_registry(missions=_Missions())
    return AgentGateway(registry=reg, authorize=dev_authz)


def test_oauth_token_reaches_the_governed_mcp_surface_end_to_end():
    srv = _server()
    gw = _gateway_with_reads()
    bridge = McpGatewayBridge(gw, srv)                        # the AS *is* the TokenVerifier
    endpoint = MCPEndpoint(bridge)
    client, verifier, code, redirect = _authorized_code(srv, Principal("agent", "service", (), "acme"))
    tok = srv.exchange(code=code, code_verifier=verifier, client_id=client.client_id,
                       redirect_uri=redirect)
    auth = f"Bearer {tok.access_token}"

    listed = endpoint.handle(auth, "tools/list")
    assert "missions.explain" in {t["name"] for t in listed["tools"]}
    called = endpoint.handle(auth, "tools/call", {"name": "missions.explain",
                                                  "arguments": {"mission_id": "m1"}})
    assert called["status"] == "ok" and called["output"]["plan"] == ["a"]

    # revoke → the same endpoint call is now unauthenticated
    srv.revoke(tok.access_token)
    with pytest.raises(GatewayAuthError):
        endpoint.handle(auth, "tools/list")


def test_endpoint_requires_a_bearer_token():
    srv = _server()
    endpoint = MCPEndpoint(McpGatewayBridge(_gateway_with_reads(), srv))
    with pytest.raises(GatewayAuthError):
        endpoint.handle(None, "tools/list")
    with pytest.raises(GatewayAuthError):
        endpoint.handle("Basic abc", "tools/list")            # not a bearer
