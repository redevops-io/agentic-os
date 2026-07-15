"""Revenue Rescue — a cross-app mission over the REAL app-operator capabilities.

Proves the runtime plans + gates + runs the Revenue Rescue workflow (agentic-apps-overview #2)
across billing → support → lifecycle → books, with the money-moving dunning step parked as a
human approval before it fires. The fleet here mirrors the capabilities the app operators
actually declare (name · provides · gate); their real handlers are tested app-side (apps/).
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import Operator, LocalOperatorClient, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

GRANTS = ["billing:write", "support:write", "lifecycle:write", "books:write"]


def build_revenue_rescue_fleet():
    """The four operators Revenue Rescue threads, with the real capability names/provides/gates."""
    billing = Operator("agentic-billing", [
        capability("billing.dunning", lambda i: {"status": "done", "overdue_count": 2},
                   provides=["dunning_attempted"], side_effecting=True, approval_required=True,
                   permissions=["billing:write"], estimated_value="high", latency_ms=1500),
    ])
    support = Operator("agentic-support", [
        capability("support.draft_reply", lambda i: {"reply_drafted": True},
                   provides=["reply_drafted"], side_effecting=True,
                   permissions=["support:write"], latency_ms=1200),
    ])
    lifecycle = Operator("lifecycle", [
        capability("lifecycle.compose_campaign", lambda i: {"campaign": "winback", "status": "draft"},
                   provides=["campaign_drafted"], side_effecting=True,
                   permissions=["lifecycle:write"], latency_ms=1000),
    ])
    books = Operator("agentic-books", [
        capability("books.reconcile", lambda i: {"reconciliation_staged": True},
                   provides=["reconciliation_staged"], permissions=["books:write"], latency_ms=600),
    ])
    ops = {op.name: op for op in (billing, support, lifecycle, books)}
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    return reg, LocalOperatorClient(ops)


def _runtime(store=None):
    reg, client = build_revenue_rescue_fleet()
    return MissionRuntime(reg, Executor(client), store=store or EventStore())


def test_revenue_rescue_end_to_end_with_money_gate():
    rt = _runtime()
    m = rt.create_mission("Recover a failed payment", policy_refs=GRANTS, template="revenue_rescue")
    rt.run(m.id)

    # the money-moving dunning step parks as a human approval before it fires
    assert m.state == MissionState.WAITING_HUMAN
    pending = rt.repo.pending_human(m.id)
    assert pending and pending["capability"] == "billing.dunning"

    rt.approve(m.id, pending["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED

    # every cross-app outcome landed in world state
    world = rt._world(m.id).snapshot()
    assert {"dunning_attempted", "reply_drafted", "campaign_drafted", "reconciliation_staged"} <= set(world)


def test_revenue_rescue_reject_stops_before_money_moves():
    rt = _runtime()
    m = rt.create_mission("Recover a failed payment", policy_refs=GRANTS, template="revenue_rescue")
    rt.run(m.id)
    node_id = rt.repo.pending_human(m.id)["node_id"]

    rt.approve(m.id, node_id, "reject")
    assert rt._missions[m.id].state == MissionState.FAILED
    # nothing downstream ran — the reach-out / win-back / books adjust never fired
    world = rt._world(m.id).snapshot()
    assert "reply_drafted" not in world and "campaign_drafted" not in world


def test_revenue_rescue_needs_grants():
    """The compiler fails closed without the fleet's write grants."""
    from agentic_os.mission.compiler import compile_intent, CompileError
    from agentic_os.mission.templates import revenue_rescue
    from agentic_os.mission.types import Mission
    import pytest

    reg, _ = build_revenue_rescue_fleet()
    m = Mission(goal="rescue", policy_refs=["billing:write"])  # missing support/lifecycle/books
    with pytest.raises(CompileError) as ei:
        compile_intent(m, revenue_rescue(m.id), reg)
    assert "permission denied" in str(ei.value)
