"""The node-admission (``_pre_exec_ok``) and metering (``_meter``) seams.

Both are additive overlay hooks on MissionRuntime: the open-core default admits every node and meters
nothing, so the kernel's behaviour is unchanged (the rest of the suite is the regression proof). An overlay
(the enterprise runtime) overrides ``_pre_exec_ok`` to enforce deny-by-default identity/tenancy/budget gates
at the dispatch boundary — recording its own governance events and failing the mission fail-closed when it
refuses — and ``_meter`` to record a usage event per executed node. These tests pin the seam contract the
overlay depends on: the hook runs BEFORE any operator dispatch (no partial side effect on a refusal), on both
the serial and the concurrent wave paths, and meter fires once per executed node with the right outcome.
"""
from __future__ import annotations

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import Operator, LocalOperatorClient, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


def _types(rt, mid):
    return [e["type"] for e in rt.repo.timeline(mid)]


# ── the capability marker an overlay checks before trusting the seam ─────────
def test_runtime_advertises_the_admission_seam():
    assert MissionRuntime.SUPPORTS_ADMISSION_SEAM >= 1


# ── default: the hooks are invisible ─────────────────────────────────────────
def test_default_admits_every_node_and_meters_nothing():
    """A plain MissionRuntime behaves exactly as before: every node is admitted and the no-op _meter never
    interferes. Onboarding reaches its approval gate and, once approved, succeeds."""
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client), store=EventStore())
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN               # the compliance gate, unchanged
    pending = rt.repo.pending_human(m.id)
    rt.approve(m.id, pending["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    assert "NodeDenied" not in _types(rt, m.id)


# ── admission refusal on the SERIAL path ─────────────────────────────────────
class _Gated(MissionRuntime):
    """Overlay stand-in: refuse named capabilities at dispatch, emit a governance event, fail fail-closed."""
    def __init__(self, *a, deny=(), **k):
        super().__init__(*a, **k)
        self._deny = set(deny)
        self.admitted: list[str] = []

    def _pre_exec_ok(self, m, plan, node) -> bool:
        if node.capability in self._deny:
            self.store.append("NodeDenied", m.id,
                              {"node_id": node.id, "capability": node.capability, "reason": "test-policy"})
            self._fail(m, plan, reason=f"access denied: {node.capability}")
            return False
        self.admitted.append(node.capability)
        return True


def test_refusal_fails_the_mission_fail_closed_before_any_side_effect():
    reg, client = build_fleet()
    rt = _Gated(reg, Executor(client), store=EventStore(), deny={"billing.create_subscription"})
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    assert m.state == MissionState.FAILED
    assert m.outcome["reason"] == "access denied: billing.create_subscription"
    # the denied node emitted the overlay's governance event and NEVER dispatched (no NodeDispatched for it,
    # nothing produced into world) — the refusal cost no side effect
    assert "NodeDenied" in _types(rt, m.id)
    assert "subscription" not in rt._world(m.id).snapshot()
    assert "billing.create_subscription" not in rt.admitted


# ── admission refusal on the CONCURRENT wave path ────────────────────────────
def test_refusal_on_concurrent_wave_also_fails_fail_closed():
    """Onboarding wave 2 = {onboarding_sent, revenue_recorded} (both depend only on subscription), so with
    max_concurrency>1 the concurrent wave path runs. Denying a wave-2 capability must refuse there too —
    before dispatch — proving the hook guards the concurrent path, not only the serial one."""
    reg, client = build_fleet()
    rt = _Gated(reg, Executor(client), store=EventStore(), deny={"books.record_revenue"}, max_concurrency=4)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    assert m.state == MissionState.FAILED
    assert "NodeDenied" in _types(rt, m.id)
    # wave 1 (subscription) was admitted + committed; the denied wave-2 outcome never entered world
    assert "subscription" in rt._world(m.id).snapshot()
    assert "revenue_recorded" not in rt._world(m.id).snapshot()
    assert "books.record_revenue" not in rt.admitted


# ── metering fires once per executed node, with the outcome ──────────────────
class _Metered(MissionRuntime):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.meter_log: list[tuple[str, str]] = []

    def _meter(self, m, node, outcome: str) -> None:
        self.meter_log.append((node.capability, outcome))


def test_meter_records_ok_for_each_executed_node():
    reg, client = build_fleet()
    rt = _Metered(reg, Executor(client), store=EventStore())
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    pending = rt.repo.pending_human(m.id)
    rt.approve(m.id, pending["node_id"], "approve")
    caps = {c for c, _ in rt.meter_log}
    # every executed onboarding node metered exactly once, all "ok"
    assert {"billing.create_subscription", "support.send_onboarding",
            "books.record_revenue", "compliance.file_consent"} <= caps
    assert all(outcome == "ok" for _, outcome in rt.meter_log)
    assert len(rt.meter_log) == len(set(rt.meter_log))         # no double-metering


def test_meter_records_error_for_a_failed_node():
    ran: list[str] = []
    op = Operator("w", [
        capability("w.boom", lambda i: (_ for _ in ()).throw(RuntimeError("boom")),
                   provides=["done"], permissions=["w:write"]),
    ])
    reg = CapabilityRegistry()
    reg.register(op.manifest)

    class _FailPlanner:
        def plan(self, mission_id, goal, ctx):
            from agentic_os.mission.types import ExecutionIntent, IntentStep
            return ExecutionIntent(mission_id=mission_id, rationale="t",
                                   steps=[IntentStep(outcome="done", need="do the work", value_hint="high")])

    rt = _Metered(reg, Executor(LocalOperatorClient({"w": op})), store=EventStore(), planner=_FailPlanner())
    m = rt.create_mission("do the work", policy_refs=["w:write"])
    rt.run(m.id)
    assert m.state == MissionState.FAILED
    assert ("w.boom", "error") in rt.meter_log
