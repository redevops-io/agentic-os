"""Mission cost model — scores capabilities and plans like a query optimizer scores plans.

Used in three places: the compiler (pick the best capability for an outcome), the simulator
(project a plan's cost/success), and the scheduler/runtime (decide when a human gate is worth
inserting). Reuses the same value/cost/risk shape the tiered router already applies per task,
lifted to per-plan.
"""
from __future__ import annotations

from .types import CapabilitySpec, ExecutionGraph, Node

_VALUE = {"low": 1.0, "medium": 3.0, "high": 8.0}
_HUMAN_MINUTE_USD = 0.5           # notional cost of a minute of human attention
_LATENCY_USD_PER_S = 0.0002       # notional cost of wall-clock latency


def capability_score(cap: CapabilitySpec) -> float:
    """Higher is better: expected value net of cost and risk. Used to rank candidates."""
    value = _VALUE.get(cap.estimated_value, 3.0)
    money = cap.cost.usd + cap.cost.latency_ms / 1000 * _LATENCY_USD_PER_S
    money += cap.cost.human_minutes * _HUMAN_MINUTE_USD
    risk_penalty = 0.0
    if cap.side_effecting:
        risk_penalty += 1.0 * (1.0 - cap.confidence)     # risky writes cost more
    return cap.confidence * value - money - risk_penalty


def rank_candidates(candidates: list[CapabilitySpec]) -> list[CapabilitySpec]:
    return sorted(candidates, key=capability_score, reverse=True)


def graph_cost(graph: ExecutionGraph) -> dict:
    """Aggregate expected cost/success over a compiled graph (for the simulator)."""
    usd = 0.0
    latency = 0
    approvals = 0
    p_success = 1.0
    for n in graph.nodes:
        usd += n.cost.usd
        latency += n.cost.latency_ms          # upper bound: sequential; scheduler parallelism lowers it
        if n.approval_required:
            approvals += 1
        usd += n.cost.human_minutes * _HUMAN_MINUTE_USD
    # success = product of per-node confidences (independent-failure upper bound)
    return {"usd": round(usd, 4), "latency_ms": latency, "approvals": approvals}
