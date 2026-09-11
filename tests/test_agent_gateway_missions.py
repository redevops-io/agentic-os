"""Governed mission delegation — Phase 2 (plan §4, §13 Phase-2 acceptance).

An external agent delegates a goal through the gateway; the Mission Runtime executes it internally
and gates its own side effects (it returns parked at a human gate, not pushed past it). The adapter
is a thin mapping, so a faithful runtime double proves the wiring + the governed surfacing.
"""
from __future__ import annotations

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, DevTokenVerifier, GatewayPrincipal, GatewayRequest, GatewayStatus,
    MissionRuntimeAdapter, McpGatewayBridge, register_mission_capabilities)
from agentic_os.agent_gateway.registry import CapabilityRegistry


# ── a faithful MissionRuntime double (same public contract the adapter uses) ────────
class _St:
    def __init__(self, name): self.name = name


class _Mission:
    def __init__(self, mid, state): self.id = mid; self.state = _St(state)


class _FakeRuntime:
    """Mimics MissionRuntime.create_mission/run/missions/inbox/explain/suspend/resume."""
    def __init__(self, run_state="WAITING_HUMAN"):
        self.run_state = run_state
        self.created, self.ran, self.suspended, self.resumed = [], [], [], []
    def create_mission(self, goal, *, constraints=None):
        self.created.append((goal, tuple(constraints or ())))
        return _Mission("mission:1", "PLANNED")
    def run(self, mid):
        self.ran.append(mid)
        return _Mission(mid, self.run_state)
    def missions(self):
        return [{"id": "mission:1", "state": self.run_state, "goal": "g"}]
    def inbox(self):
        return [{"mission_id": "mission:1", "node": "refund"}] if self.run_state == "WAITING_HUMAN" else []
    def explain(self, mid):
        return {"id": mid, "plan": ["research", "prepare", "approve", "send"]}
    def suspend(self, mid, *, actor, reason):
        self.suspended.append((mid, actor, reason)); return _Mission(mid, "SUSPENDED")
    def resume(self, mid, *, actor):
        self.resumed.append((mid, actor)); return _Mission(mid, "RUNNING")


def _wire(run_state="WAITING_HUMAN", grants=("missions.delegate", "missions.read", "missions.control")):
    rt = _FakeRuntime(run_state)
    adapter = MissionRuntimeAdapter(rt)
    dev = DevTokenVerifier()
    reg = register_mission_capabilities(CapabilityRegistry(), adapter)
    gw = AgentGateway(registry=reg, authorize=dev.authorize, mission=adapter)   # adapter is the MissionPort
    token = dev.issue("agent", tenant="acme", grants=grants)
    return rt, gw, dev, token, GatewayPrincipal(Principal("agent", "service", (), "acme"))


# ── delegation ──────────────────────────────────────────────────────────────────────
def test_delegate_goal_creates_runs_and_surfaces_pending_approval():
    rt, gw, _, _, gp = _wire(run_state="WAITING_HUMAN")
    r = gw.invoke(GatewayRequest(gp, "missions.delegate_goal",
                                 {"goal": "find 5 pilot prospects and prepare outreach"}))
    assert r.status is GatewayStatus.OK
    assert r.mission_id == "mission:1"
    assert r.output["needs_approval"] is True                 # runtime parked at a human gate
    assert rt.created and rt.ran == ["mission:1"]             # created AND run internally


def test_delegated_mission_that_completes_does_not_flag_approval():
    rt, gw, _, _, gp = _wire(run_state="SUCCEEDED")
    r = gw.invoke(GatewayRequest(gp, "missions.delegate_goal", {"goal": "summarize project"}))
    assert r.status is GatewayStatus.OK and r.output["needs_approval"] is False


def test_delegate_requires_permission():
    rt, gw, dev, _, _ = _wire(grants=("missions.read",))       # no missions.delegate grant
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    r = gw.invoke(GatewayRequest(gp, "missions.delegate_goal", {"goal": "x"}))
    assert r.status is GatewayStatus.DENIED                    # not visible ⇒ unknown_capability
    assert rt.created == []                                    # never reached the runtime


# ── read + control through the adapter ────────────────────────────────────────────
def test_mission_reads_flow_through_the_adapter():
    rt, gw, _, _, gp = _wire()
    assert gw.invoke(GatewayRequest(gp, "missions.list")).output[0]["id"] == "mission:1"
    assert gw.invoke(GatewayRequest(gp, "missions.explain",
                                    {"mission_id": "mission:1"})).output["plan"][-1] == "send"
    assert gw.invoke(GatewayRequest(gp, "missions.list_pending_approvals")).output[0]["node"] == "refund"


def test_pause_and_resume_carry_the_caller_as_actor():
    rt, gw, _, _, gp = _wire()
    p = gw.invoke(GatewayRequest(gp, "missions.pause", {"mission_id": "mission:1"}))
    assert p.status is GatewayStatus.OK and p.output["state"] == "SUSPENDED"
    assert rt.suspended[0][:2] == ("mission:1", "agent")       # actor = the principal subject
    rr = gw.invoke(GatewayRequest(gp, "missions.resume", {"mission_id": "mission:1"}))
    assert rr.output["state"] == "RUNNING" and rt.resumed[0] == ("mission:1", "agent")


# ── the flagship path is reachable over MCP ──────────────────────────────────────────
def test_delegate_goal_is_callable_over_the_mcp_bridge():
    rt, gw, dev, token, _ = _wire()
    bridge = McpGatewayBridge(gw, dev)
    assert "missions.delegate_goal" in {t["name"] for t in bridge.list_tools(token)}
    out = bridge.call_tool(token, "missions.delegate_goal", {"goal": "qualify inbound leads"})
    assert out["status"] == "ok" and out["mission_id"] == "mission:1"


def test_submit_approval_is_not_exposed_to_agents():
    # approving a mission gate is a human control-plane action, not an agent self-serve capability
    rt, gw, _, _, gp = _wire()
    names = {m.name for m in gw.registry.all()}
    assert "missions.submit_approval" not in names
