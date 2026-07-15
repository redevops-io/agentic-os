"""New-Customer Onboarding — the seed cross-app mission over the REAL app-operator capabilities.

The apps grew the onboarding actions (billing.create_subscription · support.send_onboarding ·
books.record_revenue · compliance.file_consent), each providing the outcome the existing
`onboarding` template already expects — so the template runs over the real operators unchanged.
compliance.file_consent is the human gate; billing/books carry saga undos so a rejected consent
rolls back the committed subscription + revenue newest-first. Real handlers are tested app-side.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import Operator, LocalOperatorClient, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


def build_onboarding_fleet(consent_fails: bool = False):
    """The four operators onboarding threads, with the real capability names/provides/gates/undos.
    Undo handlers record into `undone` so a rollback is observable. With consent_fails=True the
    consent step raises when executed (post-approval), to exercise the saga unwind."""
    undone: list[str] = []

    def _file_consent(_i):
        if consent_fails:
            raise RuntimeError("consent registry unavailable")
        return {"consent_id": "gdpr_1"}

    billing = Operator("agentic-billing", [
        capability("billing.create_subscription", lambda i: {"subscription_id": "sub_1", "plan": "pro"},
                   provides=["subscription"], side_effecting=True, undo="billing.cancel_subscription",
                   permissions=["billing:write"], estimated_value="high", latency_ms=1200),
        capability("billing.cancel_subscription",
                   lambda i: (undone.append("billing.cancel_subscription"), {"cancelled": True})[1],
                   side_effecting=True, permissions=["billing:write"]),
    ])
    support = Operator("agentic-support", [
        capability("support.send_onboarding", lambda i: {"onboarding_sent": True},
                   provides=["onboarding_sent"], side_effecting=True,
                   permissions=["support:write"], latency_ms=500),
    ])
    books = Operator("agentic-books", [
        capability("books.record_revenue", lambda i: {"entry": "je_1"},
                   provides=["revenue_recorded"], side_effecting=True, undo="books.reverse_entry",
                   permissions=["books:write"], latency_ms=400),
        capability("books.reverse_entry",
                   lambda i: (undone.append("books.reverse_entry"), {"reversed": True})[1],
                   side_effecting=True, permissions=["books:write"]),
    ])
    compliance = Operator("agentic-compliance", [
        capability("compliance.file_consent", _file_consent,
                   provides=["consent_filed"], side_effecting=True, approval_required=True,
                   permissions=["compliance:write"], estimated_value="high", latency_ms=300),
    ])
    ops = {op.name: op for op in (billing, support, books, compliance)}
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    return reg, LocalOperatorClient(ops), undone


def test_onboarding_end_to_end_over_real_operators():
    reg, client, _ = build_onboarding_fleet()
    rt = MissionRuntime(reg, Executor(client), store=EventStore())
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)

    # filing the consent record is the human gate
    assert m.state == MissionState.WAITING_HUMAN
    pending = rt.repo.pending_human(m.id)
    assert pending and pending["capability"] == "compliance.file_consent"

    rt.approve(m.id, pending["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    world = rt._world(m.id).snapshot()
    assert {"subscription", "onboarding_sent", "revenue_recorded", "consent_filed"} <= set(world)


def test_onboarding_reject_unwinds_committed_effects():
    """Rejecting the consent gate fails the mission AND unwinds the already-committed
    subscription + revenue newest-first via their saga undos — a rejected gate rolls back."""
    reg, client, undone = build_onboarding_fleet()
    rt = MissionRuntime(reg, Executor(client), store=EventStore())
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    node_id = rt.repo.pending_human(m.id)["node_id"]

    rt.approve(m.id, node_id, "reject")
    assert rt._missions[m.id].state == MissionState.FAILED
    assert "consent_filed" not in rt._world(m.id).snapshot()
    assert set(undone) == {"billing.cancel_subscription", "books.reverse_entry"}
    comp = [e for e in rt.repo.timeline(m.id) if e["type"] == "NodeCompensated"]
    assert len(comp) == 2


def test_onboarding_failure_after_approval_compensates_committed_effects():
    """Approve the consent gate, but filing fails on execution → the saga unwinds the already-
    committed subscription + revenue newest-first via their undo capabilities."""
    reg, client, undone = build_onboarding_fleet(consent_fails=True)
    rt = MissionRuntime(reg, Executor(client), store=EventStore())
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    node_id = rt.repo.pending_human(m.id)["node_id"]

    rt.approve(m.id, node_id, "approve")
    assert rt._missions[m.id].state == MissionState.FAILED
    # billing.create_subscription + books.record_revenue are compensated via their undos
    assert set(undone) == {"billing.cancel_subscription", "books.reverse_entry"}
    comp = [e for e in rt.repo.timeline(m.id) if e["type"] == "NodeCompensated"]
    assert len(comp) == 2
