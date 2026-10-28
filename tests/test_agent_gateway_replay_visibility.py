"""An idempotent replay must not outlive the caller's permission to see the capability."""
from __future__ import annotations

from agentic_os.agent_gateway import (
    AgentGateway, CapabilityRegistry, DevTokenVerifier, HandlerResult, McpGatewayBridge)
from agentic_os.agent_gateway.contracts import CapabilityKind, CapabilityManifest, DataClass


def _setup():
    reg, calls = CapabilityRegistry(), []

    def reader(req, env):
        calls.append(1)
        return HandlerResult(ok=True, output={"n": len(calls)}, data_classes=(DataClass.INTERNAL,))

    reg.register(CapabilityManifest("idem.read", "reads synthetic data", CapabilityKind.DIRECT, permissions=("data.read",),
                                    idempotent=True, data_classes=(DataClass.INTERNAL,)), reader)
    verifier = DevTokenVerifier()
    token = verifier.issue("bob", tenant="t1", grants=("data.read",))
    return McpGatewayBridge(AgentGateway(registry=reg, authorize=verifier.authorize), verifier), verifier, token, calls


def test_a_replay_is_served_while_the_permission_holds():
    bridge, _, token, calls = _setup()
    first = bridge.call_tool(token, "idem.read", {"idempotency_key": "k1"})
    again = bridge.call_tool(token, "idem.read", {"idempotency_key": "k1"})
    assert first["status"] == "ok" and again["output"] == first["output"] and calls == [1]


def test_a_replay_is_refused_after_the_permission_is_revoked():
    bridge, verifier, token, calls = _setup()
    assert bridge.call_tool(token, "idem.read", {"idempotency_key": "k1"})["status"] == "ok"
    verifier._grants[("t1", "bob")].clear()
    replay = bridge.call_tool(token, "idem.read", {"idempotency_key": "k1"})
    assert replay["status"] == "denied" and "output" not in replay and calls == [1]
