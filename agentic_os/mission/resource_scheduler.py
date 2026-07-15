"""P11 — Cross-Mission Resource Scheduling: fair-share + admission control across missions.

Per-mission concurrency (P0) is enough to *validate* the architecture; this plane matters once
multiple durable missions compete for genuinely scarce resources — operators, models, GPUs, API
quotas, monetary budget and (the one people forget) **human-approval capacity**. It arbitrates with
priorities, deadlines, organizational fair-share and starvation prevention.

Only SCARCE resources go in the pool; anything unpooled is treated as unlimited, so a mission is
never blocked on a resource nobody is rationing. Everything here is deterministic (a logical round
counter, no wall clock) so replay reproduces the same admission order.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ResourceRequest:
    mission_id: str
    demand: dict                       # resource -> amount (only pooled keys are rationed)
    priority: int = 0                  # higher = more important
    deadline: float | None = None      # earliest-deadline-first; lower = sooner (None = no deadline)
    owner: str = "default"             # fair-share tenant (org / team / customer)


@dataclass
class _Waiting:
    req: ResourceRequest
    enqueued: int                      # logical submit sequence (FIFO tiebreak)
    rounds_waited: int = 0


class InfeasibleRequest(ValueError):
    """Demand exceeds total pool capacity for some resource — it could never be admitted."""


class CrossMissionScheduler:
    def __init__(self, pools: dict, *, aging_rate: float = 1.0):
        self.capacity = dict(pools)
        self.available = dict(pools)
        self.aging_rate = aging_rate
        self._q: list[_Waiting] = []
        self._seq = 0
        self._admitted: dict[str, dict] = {}                 # mission_id -> reserved demand
        self._served: dict[str, float] = defaultdict(float)  # owner -> cumulative units granted

    # ── queue ────────────────────────────────────────────────────────────────
    def submit(self, req: ResourceRequest) -> None:
        for r, amt in req.demand.items():
            if r in self.capacity and amt > self.capacity[r]:
                raise InfeasibleRequest(f"{req.mission_id} needs {amt} {r} > capacity {self.capacity[r]}")
        self._seq += 1
        self._q.append(_Waiting(req, self._seq))

    # ── admission control ─────────────────────────────────────────────────────
    def admit(self) -> list[str]:
        """One admission round. Age every waiter (starvation prevention), then greedily admit the
        best-ranked requests that still fit — fair-share first, then aged priority, then EDF, FIFO."""
        for w in self._q:
            w.rounds_waited += 1
        admitted: list[str] = []
        for w in sorted(self._q, key=self._rank):
            if self._fits(w.req.demand):
                self._reserve(w.req.demand)
                self._admitted[w.req.mission_id] = w.req.demand
                self._served[w.req.owner] += self._weight(w.req.demand)
                admitted.append(w.req.mission_id)
        if admitted:
            self._q = [w for w in self._q if w.req.mission_id not in self._admitted]
        return admitted

    def _rank(self, w: _Waiting):
        effective_priority = w.req.priority + self.aging_rate * w.rounds_waited
        return (
            self._served[w.req.owner],                                   # 1. least-served owner (fair share)
            -effective_priority,                                        # 2. higher aged priority
            w.req.deadline if w.req.deadline is not None else float("inf"),  # 3. earliest deadline
            w.enqueued,                                                 # 4. FIFO
        )

    def release(self, mission_id: str) -> None:
        demand = self._admitted.pop(mission_id, None)
        if demand:
            for r, amt in demand.items():
                if r in self.capacity:
                    self.available[r] = min(self.capacity[r], self.available.get(r, 0) + amt)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _fits(self, demand: dict) -> bool:
        return all(self.available.get(r, float("inf")) >= amt for r, amt in demand.items())

    def _reserve(self, demand: dict) -> None:
        for r, amt in demand.items():
            if r in self.capacity:
                self.available[r] -= amt

    @staticmethod
    def _weight(demand: dict) -> float:
        return float(sum(demand.values())) or 1.0

    def snapshot(self) -> dict:
        return {"available": dict(self.available),
                "waiting": [w.req.mission_id for w in self._q],
                "admitted": list(self._admitted),
                "served": dict(self._served)}


def estimate_demand(plan) -> dict:
    """A mission's resource footprint from its compiled plan — monetary spend, human-approval
    capacity, execution slots and per-operator load. Only keys present in the pool are rationed."""
    g = plan.graph
    demand: dict = {
        "usd": round(sum(n.cost.usd for n in g.nodes), 4),
        "human_approval": sum(1 for n in g.nodes if n.approval_required),
        "slots": len(g.nodes),
    }
    for n in g.nodes:
        key = f"operator:{n.operator}"
        demand[key] = demand.get(key, 0) + 1
    return {k: v for k, v in demand.items() if v}
