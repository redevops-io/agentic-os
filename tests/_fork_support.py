"""Shared fixtures for the fork/join tests: a counting, idempotency-honouring fake operator whose effect
ledger survives a simulated restart (it models the EXTERNAL system), and runtime builders."""
from __future__ import annotations

import threading
import time

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.fork_join import BranchSpec, ForkSpec, JoinSpec, SimulatedCrash
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import Budget, CapabilitySpec, CapabilityManifest, NodeCost

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write", "w:write"]


class CountingOperator:
    """External system: executes each idempotency key at most once (the contract operators must honour);
    counts invocations and real effects so tests can assert 'no duplicate consequential action'."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self.effects: dict[str, int] = {}
        self.results: dict[str, dict] = {}
        self.seen_inputs: list[dict] = []
        self.fail_caps: set = set()
        self.fail_once: set = set()               # (capability, tag) failing on the first attempt only
        self.gates: dict[str, threading.Event] = {}  # tag -> event the call blocks on
        self.running = 0
        self.peak = 0
        self.order: list[str] = []
        self.by_tag_delay: dict[str, float] = {}
        self._fleet = build_fleet()[1]

    def invoke(self, operator, capability, inputs, idempotency_key, *, secrets=None):
        if not capability.startswith("w."):         # the demo fleet's own capabilities (plan nodes)
            return self._fleet.invoke(operator, capability, inputs, idempotency_key)
        tag = str(inputs.get("tag", ""))
        with self.lock:
            self.calls[idempotency_key] = self.calls.get(idempotency_key, 0) + 1
            self.seen_inputs.append(dict(inputs))
            self.order.append(tag)
            if idempotency_key in self.results:
                return self.results[idempotency_key]
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            gate = self.gates.get(tag)
            if gate is not None:
                gate.wait(10)
            d = self.by_tag_delay.get(tag, self.delay)
            if d:
                time.sleep(d)
            if capability in self.fail_caps or ((capability, tag) in self.fail_once
                                                and self.calls[idempotency_key] == 1
                                                and not idempotency_key.endswith("")):
                raise RuntimeError(f"{capability}/{tag} failed")
            with self.lock:
                if idempotency_key in self.results:      # another thread won the race: honour the key
                    return self.results[idempotency_key]
                self.effects[idempotency_key] = self.effects.get(idempotency_key, 0) + 1
                res = {"ok": True, "tag": tag, "key": idempotency_key}
                self.results[idempotency_key] = res
                return res
        finally:
            with self.lock:
                self.running -= 1


def build_registry(extra_caps=()):
    reg, _ = build_fleet()
    caps = [
        CapabilitySpec("w.write", "w-op", provides=["w_out"], side_effecting=True, undo="w.undo",
                       permissions=["w:write"], cost=NodeCost(usd=0.1)),
        CapabilitySpec("w.undo", "w-op", permissions=["w:write"]),
        CapabilitySpec("w.gated", "w-op", side_effecting=True, approval_required=True,
                       permissions=["w:write"], cost=NodeCost(usd=0.1)),
        CapabilitySpec("w.read", "w-op", permissions=["w:write"], concurrency_mode="read_only"),
        CapabilitySpec("w.keyed", "w-op", side_effecting=True, permissions=["w:write"],
                       concurrency_mode="side_effecting", concurrency_key="acct:{acct}"),
        CapabilitySpec("w.flaky", "w-op", permissions=["w:write"], cost=NodeCost(usd=0.01)),
        CapabilitySpec("w.nogrant", "w-op", permissions=["secret:admin"]),
        *extra_caps,
    ]
    reg.register(CapabilityManifest("w-op", caps))
    return reg


def build(store=None, op=None, *, grants=None, budget=None, fork_budget=None, tenant="", concurrency=1, extra_caps=(),
          limits=None, clock=None, poll_s=0.002):
    reg = build_registry(extra_caps)
    op = op or CountingOperator()
    rt = MissionRuntime(reg, Executor(op), store=store or EventStore(), max_concurrency=concurrency)
    if limits is not None or clock is not None:
        from agentic_os.mission.fork_join import ForkJoinController
        rt._forks = ForkJoinController(rt, limits=limits, **({"clock": clock} if clock else {}), poll_s=poll_s)
    else:
        rt.forks.poll_s = poll_s
    m = rt.create_mission("Onboard a new customer", policy_refs=grants or GRANTS, template="onboarding",
                          budget=budget or Budget(usd=100.0), tenant=tenant)
    if fork_budget is not None:               # the demo plan itself projects ~$1; tighten AFTER planning
        m.budget = Budget(usd=fork_budget)
    return rt, op, m


def spec(fork_id="f1", n=3, mode="all", k=None, cap="w.write", timeout_s=None, on_timeout="fail",
         max_concurrent=None, cancel_losers=True, **kw):
    brs = [BranchSpec(f"b{i}", cap, inputs={"tag": f"b{i}"}) for i in range(n)]
    return ForkSpec(fork_id, brs, JoinSpec(mode, k, timeout_s, on_timeout, cancel_losers),
                    max_concurrent=max_concurrent, **kw)


def types(rt, mid, prefix=""):
    return [e.type for e in rt.store.for_mission(mid) if e.type.startswith(prefix)]


class CrashingStore(EventStore):
    """Durable JSONL store that 'crashes' (raises SimulatedCrash) at the Nth append after arming — either
    BEFORE the event is persisted or AFTER."""

    def __init__(self, path, mode="after"):
        super().__init__(path)
        self.mode, self.countdown = mode, None
        self.appended = 0

    def arm(self, n):
        self.countdown, self.appended = n, 0

    dead = False

    def append(self, type, mission_id, payload):
        if self.dead:                      # a crashed process writes nothing more (orphan threads included)
            raise SimulatedCrash("dead")
        self.appended += 1
        if self.countdown is not None and self.appended == self.countdown and self.mode == "before":
            self.countdown, self.dead = None, True
            raise SimulatedCrash(f"before {type}")
        ev = super().append(type, mission_id, payload)
        if self.countdown is not None and self.appended == self.countdown and self.mode == "after":
            self.countdown, self.dead = None, True
            raise SimulatedCrash(f"after {type}")
        return ev
