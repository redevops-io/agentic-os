"""REST delegation adapter — Phase 7 (plan §7). Same governance as the MCP path, REST envelope."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway import (
    AgentGateway, DevTokenVerifier, GatewayAuthError, RestGatewayAdapter, build_read_registry)


class _Missions:
    def list(self): return [{"id": "m1", "state": "running"}]
    def get(self, mid): return {"id": mid}
    def explain(self, mid): return {"id": mid, "plan": ["a"]}
    def pending_approvals(self): return []


def _wire(grants=("missions.read",)):
    dev = DevTokenVerifier()
    gw = AgentGateway(registry=build_read_registry(missions=_Missions()), authorize=dev.authorize)
    token = dev.issue("agent", tenant="acme", grants=grants)
    return RestGatewayAdapter(gw, dev), token


def test_list_capabilities_returns_only_the_permitted_set():
    rest, token = _wire()
    caps = {c["name"] for c in rest.list_capabilities(f"Bearer {token}")["capabilities"]}
    assert "missions.explain" in caps and all(c.startswith("missions.") for c in caps)


def test_invoke_runs_through_the_governed_path():
    rest, token = _wire()
    out = rest.invoke(f"Bearer {token}", "missions.explain", {"mission_id": "m1"})
    assert out["status"] == "ok" and out["output"]["plan"] == ["a"]


def test_hidden_capability_denies_without_leaking():
    rest, token = _wire(grants=())                            # no grants
    out = rest.invoke(f"Bearer {token}", "missions.explain", {"mission_id": "m1"})
    assert out["status"] == "denied" and "output" not in out


def test_missing_or_bad_token_is_unauthorized():
    rest, _ = _wire()
    with pytest.raises(GatewayAuthError):
        rest.list_capabilities(None)
    with pytest.raises(GatewayAuthError):
        rest.invoke("Bearer not-a-real-token", "missions.list", {})
