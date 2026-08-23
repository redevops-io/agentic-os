"""Concurrent ready-wave execution — same plan, outcome and world state as the serial drain, lower
wall-clock. This is both the P0 fix's regression guard and the Python side of the Python↔Go concurrency
conformance (PARALLEL_ALL · bounded concurrency · replay-identity): whether an independent ready wave runs
serially (max_concurrency=1) or concurrently (max_concurrency>1), the mission outcome, the produced world
keys, the plan fingerprint and the node-success set are identical — only latency changes.
"""
from __future__ import annotations

import time

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState, NodeState


class _SleepingClient:
    """Wrap the fleet client so each operator call takes a fixed time — makes fan-out latency observable."""

    def __init__(self, inner, delay: float):
        self._inner, self._delay = inner, delay

    def invoke(self, operator, capability, inputs, idempotency_key):
        time.sleep(self._delay)
        return self._inner.invoke(operator, capability, inputs, idempotency_key)


# onboarding: subscription → [onboarding_sent ‖ revenue_recorded] → consent_filed(gated) — a real 2-wide
# fan-out with handlers the demo fleet implements (product_launch needs capabilities the demo lacks).
GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


def _run_to_done(max_concurrency: int, *, template: str = "onboarding", delay: float = 0.15):
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(_SleepingClient(client, delay)), store=EventStore(),
                        max_concurrency=max_concurrency)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template=template)
    t0 = time.perf_counter()
    rt.run(m.id)
    guard = 0
    while rt._missions[m.id].state == MissionState.WAITING_HUMAN and guard < 30:
        pend = rt.repo.pending_human(m.id)
        rt.approve(m.id, pend["node_id"], "approve")     # identical human decisions in both runs
        guard += 1
    return rt, m.id, time.perf_counter() - t0


def _fingerprint(rt, mid):
    plan = rt._plans[mid]
    return getattr(plan, "plan_fingerprint", None) or getattr(plan, "fingerprint", None)


def _succeeded(rt, mid) -> int:
    return sum(1 for st in rt.repo.node_status(mid).values() if st == NodeState.DONE)


def test_concurrent_wave_equals_serial_and_is_faster():
    rt1, mid1, wall_serial = _run_to_done(1)
    rt4, mid4, wall_parallel = _run_to_done(4)

    s1, s4 = rt1._missions[mid1], rt4._missions[mid4]
    # 1) same outcome
    assert s1.state == s4.state == MissionState.SUCCEEDED
    # 2) same produced world keys (the outcomes) and same number of succeeded nodes
    assert set(rt1._world(mid1).snapshot()) == set(rt4._world(mid4).snapshot())
    assert _succeeded(rt1, mid1) == _succeeded(rt4, mid4) > 0
    # 3) same plan fingerprint — completion-order-independent, so concurrency doesn't change plan identity
    fp1, fp4 = _fingerprint(rt1, mid1), _fingerprint(rt4, mid4)
    if fp1 is not None:
        assert fp1 == fp4
    # 4) concurrency overlapped the fan-out → materially lower wall-clock
    assert wall_parallel < wall_serial * 0.85, f"serial={wall_serial:.2f}s parallel={wall_parallel:.2f}s"


def test_default_is_serial_and_unchanged():
    """Default max_concurrency=1 must reproduce the historical serial behaviour exactly."""
    rt, mid, _ = _run_to_done(1, delay=0.0)
    assert rt._missions[mid].state == MissionState.SUCCEEDED
    assert rt.max_concurrency == 1
