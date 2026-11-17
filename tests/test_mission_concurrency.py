"""Concurrent ready-wave execution — same plan, outcome and world state as the serial drain, and observable
overlap when concurrency is allowed. This is both the P0 fix's regression guard and the Python side of the
Python↔Go concurrency conformance (PARALLEL_ALL · bounded concurrency · replay-identity): whether an
independent ready wave runs serially (max_concurrency=1) or concurrently (max_concurrency>1), the mission
outcome, the produced world keys, the plan fingerprint and the node-success set are identical.

Latency is deliberately not asserted. The previous version required the concurrent run to beat the serial one
by 15%, which made the gate depend on how busy the runner was: it failed on a loaded CI host at
`serial=0.60s parallel=0.88s` while the wave itself was behaving correctly. Overlap is what concurrency *is*,
so the instrumented client below measures how many operator calls were in flight at once — deterministic, and
a stronger claim than the timing one it replaces.
"""
from __future__ import annotations

import threading
import time

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState, NodeState


class _InstrumentedClient:
    """Wraps the fleet client and records peak in-flight operator calls.

    `delay` holds each call open long enough that a genuinely concurrent wave must overlap; the measurement is
    taken under a lock so the peak is a fact about the executor, not about scheduling luck in the test.
    """

    def __init__(self, inner, delay: float = 0.0):
        self._inner, self._delay = inner, delay
        self._lock = threading.Lock()
        self._inflight = 0
        self.max_inflight = 0
        self.calls = 0

    def invoke(self, operator, capability, inputs, idempotency_key):
        with self._lock:
            self._inflight += 1
            self.calls += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self._delay:
                time.sleep(self._delay)
            return self._inner.invoke(operator, capability, inputs, idempotency_key)
        finally:
            with self._lock:
                self._inflight -= 1


# onboarding: subscription → [onboarding_sent ‖ revenue_recorded] → consent_filed(gated) — a real 2-wide
# fan-out with handlers the demo fleet implements (product_launch needs capabilities the demo lacks).
GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]
FANOUT_WIDTH = 2


def _run_to_done(max_concurrency: int, *, template: str = "onboarding", delay: float = 0.15):
    reg, client = build_fleet()
    instrumented = _InstrumentedClient(client, delay)
    rt = MissionRuntime(reg, Executor(instrumented), store=EventStore(),
                        max_concurrency=max_concurrency)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template=template)
    rt.run(m.id)
    guard = 0
    while rt._missions[m.id].state == MissionState.WAITING_HUMAN and guard < 30:
        pend = rt.repo.pending_human(m.id)
        rt.approve(m.id, pend["node_id"], "approve")     # identical human decisions in both runs
        guard += 1
    return rt, m.id, instrumented



def _fingerprint(rt, mid):
    plan = rt._plans[mid]
    return getattr(plan, "plan_fingerprint", None) or getattr(plan, "fingerprint", None)


def _succeeded(rt, mid) -> int:
    return sum(1 for st in rt.repo.node_status(mid).values() if st == NodeState.DONE)


def test_concurrent_wave_equals_serial_and_overlaps():
    rt1, mid1, c1 = _run_to_done(1)
    rt4, mid4, c4 = _run_to_done(4)

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
    # 4) same work performed — concurrency must not skip a node or run one twice
    assert c1.calls == c4.calls > 0

    # 5) the wave actually overlapped, measured rather than inferred from elapsed time
    assert c1.max_inflight == 1, "max_concurrency=1 must never hold two operator calls open at once"
    assert c4.max_inflight == FANOUT_WIDTH, (
        f"a 2-wide ready wave with concurrency allowed held {c4.max_inflight} calls in flight")


def test_overlap_is_bounded_by_the_wave_and_by_the_declared_limit():
    """Concurrency cannot exceed either the width of the ready wave or the operator's cap.

    Measured on this template: peak in-flight is 1 at max_concurrency=1, 2 at 2 or above — the wave is only
    two nodes wide, so a wider pool must not manufacture extra parallelism, and the scheduler's own policy cap
    binds before the pool does at the top end.
    """
    for requested, min_effective in ((2, 2), (8, 2)):
        rt, mid, c = _run_to_done(requested)
        assert rt._missions[mid].state == MissionState.SUCCEEDED
        assert rt.effective_max_concurrency >= min_effective
        assert c.max_inflight <= min(rt.effective_max_concurrency, FANOUT_WIDTH), (
            f"requested={requested} effective={rt.effective_max_concurrency} peak={c.max_inflight}")


def test_default_is_serial_and_unchanged():
    """Default max_concurrency=1 must reproduce the historical serial behaviour exactly."""
    rt, mid, c = _run_to_done(1, delay=0.0)
    assert rt._missions[mid].state == MissionState.SUCCEEDED
    assert rt.max_concurrency == 1
    assert c.max_inflight == 1
