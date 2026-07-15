"""Simulator — dry-run a plan before committing it (EXPLAIN for missions).

Projects expected cost / success / latency / approvals from capability metadata + the current
belief state, WITHOUT executing side effects. Used to (a) choose among candidate plan revisions
and (b) gate: a plan whose projected cost or approvals exceed the mission budget never runs —
it replans or escalates. The `TopoScheduler` dry-run seeds the ordering; this adds the estimate.
"""
from __future__ import annotations

from .registry import CapabilityRegistry
from .scheduler import TopoScheduler, SchedulePolicy
from .types import ExecutionPlan, Mission, SimResult, NodeState


def simulate(plan: ExecutionPlan, mission: Mission, registry: CapabilityRegistry,
             policy: SchedulePolicy | None = None) -> SimResult:
    policy = policy or SchedulePolicy()
    graph = plan.graph
    usd = 0.0
    human_minutes = 0.0
    approvals = 0
    p_success = 1.0
    notes: list[str] = []

    for n in graph.nodes:
        cap = registry.get(n.capability)
        conf = cap.confidence if cap else 0.9
        p_success *= conf
        usd += n.cost.usd
        human_minutes += n.cost.human_minutes
        if n.approval_required:
            approvals += 1
            human_minutes += 2.0  # a review costs ~2 human-minutes
        if cap and cap.side_effecting and conf < 0.7:
            notes.append(f"low-confidence side effect: {n.capability} (p={conf})")

    # latency: sum along the critical path (longest dependency chain), not the whole graph
    latency = _critical_path_latency(plan)
    human_usd = human_minutes * 0.5

    within = (usd + human_usd) <= mission.budget.usd and human_minutes <= mission.budget.human_minutes
    if not within:
        notes.append(f"projected spend ${round(usd + human_usd, 3)} / {round(human_minutes,1)}m "
                     f"exceeds budget ${mission.budget.usd} / {mission.budget.human_minutes}m")
    return SimResult(
        expected_usd=round(usd + human_usd, 4),
        expected_success=round(p_success, 3),
        expected_latency_ms=latency,
        expected_approvals=approvals,
        within_budget=within,
        notes=notes,
    )


def _critical_path_latency(plan: ExecutionPlan) -> int:
    graph = plan.graph
    memo: dict[str, int] = {}

    def cp(nid: str) -> int:
        if nid in memo:
            return memo[nid]
        node = graph.by_id(nid)
        base = node.cost.latency_ms if node else 0
        deps = node.depends_on if node else []
        memo[nid] = base + (max((cp(d) for d in deps), default=0))
        return memo[nid]

    return max((cp(n.id) for n in graph.nodes), default=0)
