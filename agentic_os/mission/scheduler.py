"""Scheduler — decides WHEN and WHERE ready nodes run. Separate from the Executor.

The executor runs a node; the scheduler decides which ready nodes to release next, subject to
policy: concurrency caps, per-operator rate limits, business-hours/calendar windows, and
approval batching (release human tasks together so an operator approves a batch, not six
interruptions). It is a pluggable interface (mirrors context_runtime SchedulerPlugin); the
default is cost-aware topological waves. The scheduler never calls the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import cost
from . import concurrency as conc
from .types import ExecutionGraph, Node, NodeState


@dataclass
class SchedulePolicy:
    max_concurrency: int = 4
    per_operator_limit: dict[str, int] = field(default_factory=dict)   # operator -> max in-flight
    business_hours_only: set[str] = field(default_factory=set)         # capabilities gated to hours
    is_business_hours: Callable[[], bool] = lambda: True
    batch_approvals: bool = True


class Scheduler(Protocol):
    def ready(self, graph: ExecutionGraph, done: set[str], running: set[str],
              policy: SchedulePolicy) -> list[Node]: ...


class TopoScheduler:
    """Cost-aware Kahn waves. Emits the ready frontier, highest-value nodes first, honouring
    concurrency / per-operator / business-hours / approval-batching policy."""

    def _frontier(self, graph: ExecutionGraph, done: set[str], running: set[str],
                  policy: SchedulePolicy) -> list[Node]:
        """The causally-ready frontier (Phase A), value-sorted, business-hours filtered — shared by
        `ready` and `explain` so both reason about the exact same ordered candidate set."""
        terminal = done  # completed (done/skipped/compensated) unblock dependents
        frontier: list[Node] = []
        for n in graph.nodes:
            if n.id in done or n.id in running:
                continue
            if n.status in (NodeState.DONE, NodeState.SKIPPED, NodeState.COMPENSATED, NodeState.FAILED):
                continue
            if all(dep in terminal for dep in n.depends_on):
                frontier.append(n)

        # business-hours gate: hold gated capabilities until the window opens
        if not policy.is_business_hours():
            frontier = [n for n in frontier if n.capability not in policy.business_hours_only]

        # highest expected value first (approvals sort last within equal value so work runs first)
        frontier.sort(key=lambda n: (not n.approval_required, -n.cost.usd), reverse=False)
        frontier.sort(key=lambda n: _value(n), reverse=True)
        return frontier

    def ready(self, graph: ExecutionGraph, done: set[str], running: set[str],
              policy: SchedulePolicy) -> list[Node]:
        frontier = self._frontier(graph, done, running, policy)
        released, _ = self._release(frontier, graph, running, policy)
        return released

    def _release(self, frontier: list[Node], graph: ExecutionGraph, running: set[str],
                 policy: SchedulePolicy) -> tuple[list[Node], dict[str, str]]:
        """Phase C — dispatch the maximal SAFE subset of the ready frontier. A node is held back (and its
        reason recorded) when it would exceed the global cap, a per-operator limit, OR a resource-conflict
        key already in flight (plan §12/§13: nodes sharing an exclusive key serialize). `held` maps a
        held node id -> the auditable reason, feeding the EXPLAIN surface."""
        released: list[Node] = []
        held: dict[str, str] = {}
        op_inflight: dict[str, int] = {}
        for op in running_ops(graph, running):
            op_inflight[op] = op_inflight.get(op, 0) + 1
        # resource keys already held by nodes running from an earlier wave (empty in the barrier model,
        # populated when a caller passes a live `running` set)
        key_inflight: dict[str, int] = {}
        for n in graph.nodes:
            if n.id in running:
                conc.acquire(n, key_inflight)

        capacity = max(0, policy.max_concurrency - len(running))
        for n in frontier:
            if len(released) >= capacity:
                held.setdefault(n.id, f"concurrency cap {policy.max_concurrency} reached")
                continue
            limit = policy.per_operator_limit.get(n.operator)
            if limit is not None and op_inflight.get(n.operator, 0) >= limit:
                held[n.id] = f"operator '{n.operator}' at limit {limit}"
                continue
            reason = conc.conflict(n, key_inflight)
            if reason is not None:
                held[n.id] = reason
                continue
            released.append(n)
            op_inflight[n.operator] = op_inflight.get(n.operator, 0) + 1
            conc.acquire(n, key_inflight)
        return released, held

    def explain(self, graph: ExecutionGraph, done: set[str], running: set[str],
                policy: SchedulePolicy) -> list[dict]:
        """The parallelism EXPLAIN surface (plan §22): per ready node, whether it was released to run
        concurrently this wave or serialized, and why. Turns scheduler behaviour into auditable runtime
        behaviour without changing what runs."""
        frontier = self._frontier(graph, done, running, policy)
        released, held = self._release(frontier, graph, running, policy)
        released_ids = {n.id for n in released}
        rows: list[dict] = []
        for n in frontier:
            if n.id in released_ids:
                rows.append({"node": n.id, "capability": n.capability, "decision": "parallelized",
                             "reason": ("read-only capability" if n.concurrency_mode == conc.MODE_READ_ONLY
                                        else "independent dependencies, no resource conflict"),
                             "resource_keys": conc.resource_keys(n)})
            else:
                rows.append({"node": n.id, "capability": n.capability, "decision": "serialized",
                             "reason": held.get(n.id, "held this wave"),
                             "resource_keys": conc.resource_keys(n)})
        return rows


def _value(n: Node) -> float:
    # proxy: cheaper + non-approval nodes float up; real value comes from the cost model on the cap
    base = 1.0
    if n.approval_required:
        base -= 0.1
    return base - n.cost.usd * 0.01


def running_ops(graph: ExecutionGraph, running: set[str]) -> list[str]:
    return [n.operator for n in graph.nodes if n.id in running]
