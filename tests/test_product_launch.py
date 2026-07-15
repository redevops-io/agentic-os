"""The killer demo — one mission, eight apps, one execution graph, one approval.

"Launch Context Runtime v5": research (market-radar) → announce (growth-assistant) → blog (guide) →
social/email/leads/support in parallel → [approval] publish (social) → track (control-tower). Proves
the central thesis: the value is the Mission Runtime orchestrating eight apps as ONE business process.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import Operator, LocalOperatorClient, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

GRANTS = ["radar:read", "assistant:write", "guide:read", "social:write",
          "lifecycle:write", "crm:write", "support:write", "bi:read"]


def build_launch_fleet():
    """The eight apps the launch threads, each providing one step's outcome."""
    ops = {
        "market-radar": Operator("market-radar", [
            capability("radar.brief", lambda i: {"competitors": 3, "brief": "3 rivals, price gap"},
                       provides=["competitor_brief"], permissions=["radar:read"], latency_ms=4000)]),
        # the prep steps DRAFT/stage (no public side effect) → no gate; only the publish gates
        "growth-assistant": Operator("growth-assistant", [
            capability("assistant.announce", lambda i: {"copy": "Introducing Context Runtime v5…"},
                       provides=["announcement_drafted"], permissions=["assistant:write"])]),
        "guide": Operator("guide", [
            capability("guide.blog", lambda i: {"blog": "Why missions, not prompts"},
                       provides=["blog_drafted"], permissions=["guide:read"])]),
        "social-autopilot": Operator("social-autopilot", [
            capability("social.draft", lambda i: {"post": "🚀 v5 is live"},
                       provides=["social_drafted"], permissions=["social:write"]),
            capability("social.publish", lambda i: {"published": True, "channels": ["linkedin"]},
                       provides=["launch_published"], side_effecting=True, approval_required=True,
                       undo="social.unpublish", permissions=["social:write"], estimated_value="high"),
            capability("social.unpublish", lambda i: {"unpublished": True},
                       side_effecting=True, permissions=["social:write"])]),
        "lifecycle": Operator("lifecycle", [
            capability("lifecycle.email", lambda i: {"campaign": "launch", "status": "draft"},
                       provides=["email_drafted"], permissions=["lifecycle:write"])]),
        "agentic-crm": Operator("agentic-crm", [
            capability("crm.leads", lambda i: {"leads": 42, "scored": True},
                       provides=["leads_scored"], permissions=["crm:write"])]),
        "agentic-support": Operator("agentic-support", [
            capability("support.brief", lambda i: {"briefed": True},
                       provides=["support_briefed"], permissions=["support:write"])]),
        "control-tower": Operator("control-tower", [
            capability("bi.track", lambda i: {"dashboard": "launch", "conversions": 0},
                       provides=["conversions_tracked"], permissions=["bi:read"])]),
    }
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    return reg, LocalOperatorClient(ops), ops


def _runtime():
    reg, client, _ = build_launch_fleet()
    return MissionRuntime(reg, Executor(client), store=EventStore())


def test_product_launch_spans_eight_apps_with_one_approval():
    rt = _runtime()
    m = rt.create_mission("Launch Context Runtime v5", policy_refs=GRANTS, template="product_launch")
    rt.run(m.id)

    # every prep step ran; the mission parks on the single public-launch approval
    assert m.state == MissionState.WAITING_HUMAN
    pending = rt.repo.pending_human(m.id)
    assert pending and pending["capability"] == "social.publish"
    world = rt._world(m.id).snapshot()
    assert {"competitor_brief", "announcement_drafted", "blog_drafted", "social_drafted",
            "email_drafted", "leads_scored", "support_briefed"} <= set(world)   # 7 pre-launch outcomes

    rt.approve(m.id, pending["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    world = rt._world(m.id).snapshot()
    assert "launch_published" in world and "conversions_tracked" in world       # published + tracked

    # ONE graph spanning EIGHT distinct apps
    plan = rt._plans[m.id]
    operators = {n.operator for n in plan.graph.nodes}
    assert len(operators) == 8, operators
    assert len(plan.graph.nodes) == 9


def test_reject_unwinds_and_nothing_goes_live():
    rt = _runtime()
    m = rt.create_mission("Launch Context Runtime v5", policy_refs=GRANTS, template="product_launch")
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "reject")
    assert rt._missions[m.id].state == MissionState.FAILED
    assert "launch_published" not in rt._world(m.id).snapshot()   # the launch never went public
