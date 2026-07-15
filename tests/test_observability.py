"""v6 Phase 6.3 — observability spans + monitoring SLOs derived from the mission event log."""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.observability import fleet_slos, mission_spans
from agentic_os.mission.operator_sdk import Operator, LocalOperatorClient, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


def _fleet():
    return {
        "billing": Operator("billing", [
            capability("billing.create_subscription", lambda i: {"subscription_id": "sub_1"},
                       provides=["subscription"], side_effecting=True, undo="billing.cancel_subscription",
                       permissions=["billing:write"]),
            capability("billing.cancel_subscription", lambda i: {"cancelled": True},
                       side_effecting=True, permissions=["billing:write"])]),
        "support": Operator("support", [
            capability("support.send_onboarding", lambda i: {"onboarding_sent": True},
                       provides=["onboarding_sent"], side_effecting=True, permissions=["support:write"])]),
        "books": Operator("books", [
            capability("books.record_revenue", lambda i: {"entry": "je_1"},
                       provides=["revenue_recorded"], side_effecting=True, undo="books.reverse_entry",
                       permissions=["books:write"]),
            capability("books.reverse_entry", lambda i: {"reversed": True},
                       side_effecting=True, permissions=["books:write"])]),
        "compliance": Operator("compliance", [
            capability("compliance.file_consent", lambda i: {"consent_id": "gdpr_1"},
                       provides=["consent_filed"], side_effecting=True, approval_required=True,
                       permissions=["compliance:write"])]),
    }


def _runtime(store=None):
    ops = _fleet()
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    return MissionRuntime(reg, Executor(LocalOperatorClient(ops)), store=store or EventStore())


def test_mission_spans_from_event_log():
    rt = _runtime()
    m = rt.create_mission("Onboard", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED

    spans = mission_spans(rt, m.id)
    root = spans[0]
    assert root["kind"] == "mission" and root["status"] == "succeeded"
    assert root["duration_ms"] is not None and root["duration_ms"] >= 0

    node = {s["name"]: s for s in spans if s["kind"] == "node"}
    # every executed capability produced an OK span
    assert {"billing.create_subscription", "support.send_onboarding",
            "books.record_revenue", "compliance.file_consent"} <= set(node)
    assert all(s["status"] == "ok" and s["duration_ms"] >= 0 for s in node.values())


def test_fleet_slos_across_missions():
    rt = _runtime()
    # one completed, one parked awaiting approval
    a = rt.create_mission("Onboard A", policy_refs=GRANTS, template="onboarding")
    rt.run(a.id)
    rt.approve(a.id, rt.repo.pending_human(a.id)["node_id"], "approve")
    b = rt.create_mission("Onboard B", policy_refs=GRANTS, template="onboarding")
    rt.run(b.id)  # parks on the consent gate

    slos = fleet_slos(rt)
    assert slos["missions"] == 2
    assert slos["by_state"].get("succeeded") == 1 and slos["by_state"].get("waiting_human") == 1
    assert slos["success_rate"] == 0.5
    assert slos["pending_approvals"] == 1
    # the completed mission contributes a non-negative latency percentile
    assert slos["latency_p50_ms"] >= 0 and slos["latency_p95_ms"] >= 0
