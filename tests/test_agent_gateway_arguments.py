"""Malformed arguments are refused the same way on every interface, before anything runs or is spent."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from agentic_os.agent_gateway import (
    AgentGateway, CapabilityRegistry, DevTokenVerifier, HandlerResult, InMemoryAuditSink, MCPEndpoint, McpGatewayBridge,
    RestGatewayAdapter)
from agentic_os.agent_gateway.capabilities import READ_CAPABILITIES
from agentic_os.agent_gateway.contracts import CapabilityKind, CapabilityManifest, RiskTier
from agentic_os.projects_api import SampleProjectionProvider, create_app

SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"],
          "additionalProperties": False}
BAD = [({}, "query: required"), ({"query": 5}, "query: expected string"), ({"query": "x", "limit": "many"}, "limit: expected integer"),
       ({"query": "x", "surprise": 1}, "surprise: not allowed"), ({"arguments": {"query": "x"}}, "arguments: not allowed")]


class Budget:
    def __init__(self):
        self.checked = 0

    def check(self, principal, manifest):
        self.checked += 1
        return True, "", ()


def world(schema=SCHEMA):
    ran, audit, budget = [], InMemoryAuditSink(), Budget()
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("find.read", "find", CapabilityKind.DIRECT, permissions=("find.read",), input_schema=schema),
                 lambda req, env: ran.append(dict(req.arguments)) or HandlerResult(ok=True, output=[]))
    verifier = DevTokenVerifier()
    token = verifier.issue("a", tenant="t", grants=("find.read",))
    gw = AgentGateway(registry=reg, authorize=verifier.authorize, audit=audit, budget=budget)
    return gw, verifier, token, ran, audit, budget


@pytest.mark.parametrize("arguments,why", BAD)
def test_a_malformed_call_runs_nothing_spends_nothing_and_says_which_field(arguments, why):
    gw, verifier, token, ran, audit, budget = world()
    rest = RestGatewayAdapter(gw, verifier).invoke(f"Bearer {token}", "find.read", dict(arguments))
    mcp = McpGatewayBridge(gw, verifier).call_tool(token, "find.read", dict(arguments))
    shaped = MCPEndpoint(McpGatewayBridge(gw, verifier)).handle(f"Bearer {token}", "tools/call", {"name": "find.read", "arguments": dict(arguments)})
    for got in (rest, mcp, shaped):
        assert got["status"] == "error" and got["error"].startswith("invalid_arguments: ") and why in got["error"], got
    assert ran == [] and budget.checked == 0
    assert [e.status.value for e in audit.events] == ["error"] * 3


def test_a_value_is_never_echoed_back():
    gw, verifier, token, *_ = world()
    got = McpGatewayBridge(gw, verifier).call_tool(token, "find.read", {"query": 1, "limit": "SECRET-VALUE-123"})
    assert "SECRET-VALUE-123" not in str(got)


def test_good_calls_and_the_gateways_own_idempotency_key_pass():
    gw, verifier, token, ran, _, budget = world()
    rest = RestGatewayAdapter(gw, verifier)
    assert rest.invoke(f"Bearer {token}", "find.read", {"query": "x", "limit": 3})["status"] == "ok"
    assert rest.invoke(f"Bearer {token}", "find.read", {"query": "x", "idempotency_key": "abc-12345"})["status"] == "ok"
    assert len(ran) == 2 and budget.checked == 2


def test_a_partial_schema_still_checks_what_it_names_and_accepts_the_rest():
    """An integrator's schema that does not close itself keeps working (a voice capability lists two of its fields)."""
    gw, verifier, token, ran, *_ = world({k: v for k, v in SCHEMA.items() if k != "additionalProperties"})
    assert McpGatewayBridge(gw, verifier).call_tool(token, "find.read", {"query": 5, "budget_band": "x"})["error"].startswith("invalid_arguments: query")
    assert McpGatewayBridge(gw, verifier).call_tool(token, "find.read", {"query": "x", "budget_band": "x"})["status"] == "ok"
    gw, verifier, token, ran, *_ = world({**SCHEMA, "additionalProperties": True})
    assert McpGatewayBridge(gw, verifier).call_tool(token, "find.read", {"query": "x", "extra": 1})["status"] == "ok"
    gw, verifier, token, ran, *_ = world({})
    assert McpGatewayBridge(gw, verifier).call_tool(token, "find.read", {"anything": 1})["status"] == "ok"


def test_a_caller_who_may_not_see_a_capability_learns_nothing_about_its_inputs():
    gw, verifier, _, ran, *_ = world()
    stranger = verifier.issue("b", tenant="t", grants=())
    assert McpGatewayBridge(gw, verifier).call_tool(stranger, "find.read", {"query": 5})["status"] == "denied"


def test_every_mounted_read_declares_a_contract_and_the_peers_six_calls_are_refused(monkeypatch):
    for k in ("AGENTIC_OS_PROFILE", "AGENT_GATEWAY_AGENTS_FILE", "AGENTIC_OS_TENANT_LIMITS", "AGENTIC_OS_OAUTH_DB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", "t-shared-value")
    from agentic_os.agent_gateway.product_capabilities import manifests as product_manifests
    for m in (*READ_CAPABILITIES, *product_manifests()):               # everything the mounted gateway can offer is closed
        assert m.input_schema.get("additionalProperties") is False, m.name
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    who = {"Authorization": "Bearer t-shared-value"}

    def call(name, body):
        return c.post(f"/api/agent-gateway/capabilities/{name}/invoke", headers=who, json=body).json()

    for name, body in (("sources.search", {}), ("sources.search", {"query": 5}), ("sources.search", {"query": "x", "limit": "many"}),
                       ("projects.get_status", {"project_id": 7}), ("projects.list", {"surprise": 1}), ("missions.get", {})):
        got = call(name, body)
        assert got["status"] == "error" and got["error"].startswith("invalid_arguments: "), (name, body, got)
    for name, body in (("projects.get_status", {}), ("projects.list", {}), ("sources.search", {"query": "x"}), ("missions.list", {})):
        assert call(name, body)["status"] == "ok", (name, body)


def test_under_authority_a_malformed_call_returns_what_it_reserved_and_an_unauthorized_one_learns_nothing_else(tmp_path):
    from agentic_os.mission.authority import (AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, SqliteEnvelopeStore, SqliteLedger)
    reg, ran = CapabilityRegistry(), []
    reg.register(CapabilityManifest("find.read", "find", CapabilityKind.DIRECT, permissions=("find.read",), input_schema=SCHEMA),
                 lambda req, env: ran.append(1) or HandlerResult(ok=True, output=[]))
    verifier = DevTokenVerifier()
    token = verifier.issue("a", tenant="t", grants=("find.read",))
    store, ledger = SqliteEnvelopeStore(str(tmp_path / "a.db")), SqliteLedger(str(tmp_path / "a.db") + ".usage")
    gw = AgentGateway(registry=reg, authorize=verifier.authorize, authority=AuthorityGuard(store, ledger), authority_key=lambda p: "key-a")
    store.put(AuthorityEnvelope(tenant="t", principal="a", mission_id="key-a", grants=(Grant("find.read", max_tier=0),),
                                budget=AutonomyBudget(usd=0, max_actions=2), issued_at=0, expires_at=4_000_000_000))
    call = McpGatewayBridge(gw, verifier).call_tool
    for _ in range(5):                                                       # more malformed calls than the whole budget
        assert call(token, "find.read", {"query": 5})["error"].startswith("invalid_arguments")
    assert call(token, "find.read", {"query": "x"})["status"] == "ok" and call(token, "find.read", {"query": "y"})["status"] == "ok"
    assert len(ran) == 2
    store.revoke("key-a")
    assert call(token, "find.read", {"query": 5})["status"] == "denied"      # no authority: the answer says that, not what was wrong
