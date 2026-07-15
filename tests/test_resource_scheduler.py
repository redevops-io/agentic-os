"""P11 — cross-mission resource scheduling: fair-share, priority, EDF, admission, starvation.

Deterministic (logical rounds, no wall clock), so replay reproduces the admission order.
"""
from __future__ import annotations

import pytest

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.resource_scheduler import (
    CrossMissionScheduler, InfeasibleRequest, ResourceRequest, estimate_demand,
)
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
)


def _req(mid, *, owner="default", priority=0, deadline=None, slots=1):
    return ResourceRequest(mid, {"slots": slots}, priority=priority, deadline=deadline, owner=owner)


def _drain(sched) -> list[str]:
    """Admit/release one at a time to observe the full ordering under a tight pool."""
    order = []
    while True:
        admitted = sched.admit()
        if not admitted:
            break
        for mid in admitted:
            order.append(mid)
            sched.release(mid)
    return order


# ─── fair-share interleaves owners regardless of submit order ─────────────────
def test_fair_share_interleaves_owners():
    s = CrossMissionScheduler({"slots": 1})
    s.submit(_req("A1", owner="A"))
    s.submit(_req("A2", owner="A"))
    s.submit(_req("B1", owner="B"))
    # A got served once, so B (least-served) jumps ahead of A's second request.
    assert _drain(s) == ["A1", "B1", "A2"]


# ─── higher priority wins under contention ────────────────────────────────────
def test_priority_ordering():
    s = CrossMissionScheduler({"slots": 1})
    s.submit(_req("low", priority=0))
    s.submit(_req("high", priority=5))
    assert _drain(s) == ["high", "low"]


# ─── earliest-deadline-first among equal owner/priority ───────────────────────
def test_earliest_deadline_first():
    s = CrossMissionScheduler({"slots": 1})
    s.submit(_req("later", deadline=9.0))
    s.submit(_req("sooner", deadline=2.0))
    assert _drain(s) == ["sooner", "later"]


# ─── admission control: a request that doesn't fit waits; smaller ones backfill ─
def test_admission_backfills_when_big_request_waits():
    s = CrossMissionScheduler({"slots": 2})
    s.submit(_req("big", slots=2, priority=10))     # fits alone
    s.submit(_req("small", slots=1, priority=1))
    first = s.admit()
    assert first == ["big"]                          # big outranks and consumes both slots
    assert "small" in s.snapshot()["waiting"]        # small couldn't fit alongside — waits
    s.release("big")
    assert s.admit() == ["small"]


# ─── starvation prevention: aging lets a long-waiter overtake a fresh higher-priority one ─
def test_aging_prevents_starvation():
    s = CrossMissionScheduler({"slots": 1}, aging_rate=1.0)
    s.submit(_req("blocker", owner="X"))
    s.admit()                                        # blocker holds the only slot
    s.submit(_req("patient", owner="Y", priority=0))
    for _ in range(5):
        s.admit()                                    # 'patient' ages while it can't fit
    s.submit(_req("fresh", owner="Y", priority=3))   # higher priority but just arrived
    s.release("blocker")
    admitted = s.admit()
    assert admitted == ["patient"]                   # aged priority (>6) beat fresh priority 3


# ─── an impossible demand is rejected at submit, not silently starved forever ──
def test_infeasible_request_rejected():
    s = CrossMissionScheduler({"slots": 2})
    with pytest.raises(InfeasibleRequest):
        s.submit(_req("huge", slots=3))


# ─── demand is estimated from the compiled plan ───────────────────────────────
def _single_fleet():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec("op.x", "op", provides=["done"])]))
    return reg, InMemoryOperatorClient({"op.x": lambda i: {"ok": 1}})


class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome="done", need="x")])


def test_estimate_demand_from_plan():
    reg, client = _single_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    m = rt.create_mission("job", policy_refs=["*"])
    d = estimate_demand(rt._plans[m.id])
    assert d["slots"] == 1 and d["operator:op"] == 1


# ─── run_batch arbitrates real missions under a scarce operator pool ──────────
def test_run_batch_orders_by_priority_under_scarcity():
    reg, client = _single_fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_P())
    a = rt.create_mission("job", policy_refs=["*"])
    b = rt.create_mission("job", policy_refs=["*"])
    # only one op-slot at a time → the arbiter serialises; b outranks a by priority
    out = rt.run_batch([a.id, b.id], pools={"operator:op": 1}, priorities={b.id: 5})
    assert out["order"] == [b.id, a.id]
    assert rt.repo.state(a.id) == MissionState.SUCCEEDED
    assert rt.repo.state(b.id) == MissionState.SUCCEEDED
    assert not out["unscheduled"]
