"""HTTP operator bridge — discover + invoke deployed agents' capabilities, credential-free."""
from __future__ import annotations

import json

import pytest

from agentic_os.mission.bridge import BridgeError, HTTPOperatorClient, build_bridge, discover_capabilities

SECRET = "DO-NOT-FORWARD-secret"


def _fake_agents():
    """Two agents publish an operator; one publishes none (skipped). Captures invoke bodies for assertions."""
    seen = {"bodies": []}

    def t(method, url, body, headers):
        if url.endswith("/capabilities"):
            if "/revenue" in url:
                return {"operator": "revenue", "capabilities": [
                    {"name": "crm.company.read", "operator": "revenue", "inputs": {}, "outputs": {"connected": "bool"}},
                    {"name": "crm.opportunity.create", "operator": "revenue", "side_effecting": True,
                     "approval_required": True, "required_authority": ["crm:write"]}]}
            if "/finance" in url:
                return {"operator": "agentic-finance", "capabilities": [
                    {"name": "finance.investigate", "operator": "agentic-finance"}]}
            raise RuntimeError("404 no operator")     # the third agent has no operator surface
        if url.endswith("/invoke"):
            b = json.loads(body); seen["bodies"].append((url, b))
            return {"result": {"invoked": b["capability"], "at": url}}
        return {}
    return t, seen


AGENTS = {"revenue": "http://revenue-agent:8220/", "finance": "http://finance-agent:8231",
          "content": "http://content-agent:8222"}


def test_discovery_lists_only_agents_with_an_operator():
    t, _ = _fake_agents()
    caps, bases = discover_capabilities(AGENTS, transport=t)
    names = {c.name for c in caps}
    assert names == {"crm.company.read", "crm.opportunity.create", "finance.investigate"}   # content skipped
    assert bases["revenue"].endswith("revenue-agent:8220") and "finance" in bases["agentic-finance"]
    write = next(c for c in caps if c.name == "crm.opportunity.create")
    assert write.side_effecting and write.approval_required and "crm:write" in write.required_authority


def test_invoke_routes_to_the_right_agent():
    t, seen = _fake_agents()
    _caps, client = build_bridge(AGENTS, transport=t)
    r = client.invoke("revenue", "crm.company.read", {"account": "Acme"})
    assert r == {"invoked": "crm.company.read"} or r.get("invoked") == "crm.company.read"
    assert seen["bodies"][0][0].endswith("/invoke") and "revenue-agent" in seen["bodies"][0][0]


def test_bridge_never_forwards_secrets():
    """A deployed agent resolves its OWN credentials; the bridge must not send any secret over the wire."""
    t, seen = _fake_agents()
    _c, client = build_bridge(AGENTS, transport=t)
    client.invoke("revenue", "crm.company.read", {"x": 1}, secrets={"twenty": SECRET})
    body_blob = json.dumps(seen["bodies"])
    assert SECRET not in body_blob and "secrets" not in seen["bodies"][0][1]


def test_unknown_operator_errors():
    t, _ = _fake_agents()
    _c, client = build_bridge(AGENTS, transport=t)
    with pytest.raises(BridgeError):
        client.invoke("nope", "x", {})


def test_agent_result_is_unwrapped():
    t, _ = _fake_agents()
    _c, client = build_bridge(AGENTS, transport=t)
    # the agent router returns {"result": {...}}; the client unwraps to the plain result
    assert "result" not in client.invoke("agentic-finance", "finance.investigate", {})
