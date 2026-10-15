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


def _run_onboarding(action, consent_fails=False, **create):
    reg, client, undone = build_onboarding_fleet(consent_fails=consent_fails)
    rt = MissionRuntime(reg, Executor(client), store=EventStore())
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", **create)
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], action)
    return rt, m, undone


def _assert_preserved(rt, m, undone):
    """onboarding is PRESERVE_ONLY: created accounts / configurations / completed setup are KEPT and the incomplete
    onboarding is reported, never silently reversed."""
    from agentic_os.mission.compensation_runner import attention_items
    assert rt._missions[m.id].state == MissionState.FAILED
    assert undone == []
    types = [e["type"] for e in rt.repo.timeline(m.id)]
    assert not any(t.startswith("Compensation") or t == "NodeCompensated" for t in types)
    out = rt._missions[m.id].outcome
    assert out["failure_policy"] == "PRESERVE_ONLY" and out["rolled_back"] is False
    assert out["partially_completed"] is True
    kept = {r["capability"] for r in out["retained_effects"]}
    assert {"billing.create_subscription", "books.record_revenue"} <= kept
    assert {c["capability"] for c in out["completed_steps"]} >= {"billing.create_subscription",
                                                                 "support.send_onboarding", "books.record_revenue"}
    assert [i["capability"] for i in out["incomplete_steps"]] == ["compliance.file_consent"]
    assert out["recovery_actions"]
    assert [i for i in attention_items(rt, m.id) if i["kind"] == "effects_left_in_place"]


def test_onboarding_reject_preserves_committed_effects_and_reports_incomplete_onboarding():
    rt, m, undone = _run_onboarding("reject")
    assert "consent_filed" not in rt._world(m.id).snapshot()
    _assert_preserved(rt, m, undone)


def test_onboarding_failure_after_approval_preserves_committed_effects():
    rt, m, undone = _run_onboarding("approve", consent_fails=True)
    _assert_preserved(rt, m, undone)


def test_onboarding_reject_unwinds_committed_effects_only_when_opted_in():
    rt, m, undone = _run_onboarding("reject", failure_policy="COMPENSATE_DECLARED")
    assert rt._missions[m.id].state == MissionState.FAILED
    assert "consent_filed" not in rt._world(m.id).snapshot()
    assert set(undone) == {"billing.cancel_subscription", "books.reverse_entry"}
    comp = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationExecuted"]
    assert len(comp) == 2


def test_onboarding_failure_after_approval_compensates_when_opted_in():
    rt, m, undone = _run_onboarding("approve", consent_fails=True, failure_policy="COMPENSATE_DECLARED")
    assert rt._missions[m.id].state == MissionState.FAILED
    assert set(undone) == {"billing.cancel_subscription", "books.reverse_entry"}
    comp = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationExecuted"]
    assert len(comp) == 2


def test_onboarding_restart_resume_does_not_repeat_completed_steps():
    """Counting client OUTSIDE the log: a new runtime on the same log resumes the consent gate without re-running
    the already-committed subscription / welcome / revenue steps."""
    reg, client, _ = build_onboarding_fleet()
    counts: dict = {}

    class CC:
        def invoke(self, operator, capability, inputs, key, **kw):
            counts[capability] = counts.get(capability, 0) + 1
            return client.invoke(operator, capability, inputs, key, **kw)
    store = EventStore()
    rt = MissionRuntime(reg, Executor(CC()), store=store)
    m = rt.create_mission("Onboard", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    rt2 = MissionRuntime(reg, Executor(CC()), store=store)
    rt2.rehydrate(m.id)
    rt2.approve(m.id, rt2.repo.pending_human(m.id)["node_id"], "approve")
    assert rt2._missions[m.id].state == MissionState.SUCCEEDED
    assert counts == {"billing.create_subscription": 1, "support.send_onboarding": 1,
                      "books.record_revenue": 1, "compliance.file_consent": 1}
