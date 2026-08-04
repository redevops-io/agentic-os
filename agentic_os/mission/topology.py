"""Execution topology — the seventh planning dimension (Whitepaper v8 / v9 seed). [public contract]

Context Runtime already optimizes representation, retrieval, reasoning, model, verification, and (v8)
which mission and how much autonomy. This adds one more: *how the work is organized* — single agent,
coordinator + workers, voting, pipeline, or hierarchical. The empirical finding (from evaluating hundreds
of agent configurations): parallel topologies win when work decomposes into independent subtasks; a single
capable agent wins on tightly-coupled sequential work, where coordination overhead and error propagation
outweigh the parallelism. Mission Runtime still owns the merge, verification, approvals, replay and audit,
so a parallel topology stays a governed mission.

This module is the public contract: the topology **model** (`ExecutionTopology`, `TaskShape`,
`TopologyProjection`, `TopologyWeights`), **validation**, and a **deterministic rule-based selector**. The
cost-model optimizer (`_project` scoring) and the learned per-mission-class success prior are an enterprise
overlay, registered via :func:`set_default_optimizer`; a public-only deploy selects the topology
deterministically from the `TaskShape`. Same pattern as ``merge()``: a working default in public, the
learned/optimizing implementation injected as an overlay.

Contract version: ``topology/v8`` — the version is the whitepaper that specifies the dimension (v8, the
seventh planning axis). Additive within that lineage (new topology kinds, new optional fields); a later
whitepaper that redefines the model bumps the version. The ``_demo`` self-test is the golden fixture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .types import new_id

CONTRACT_VERSION = "topology/v8"
TOPOLOGY_KINDS = ("single_agent", "coordinator_workers", "voting", "pipeline", "hierarchical")
MERGE_POLICIES = ("verify_then_commit", "majority_vote", "sequential")


@dataclass
class ExecutionTopology:
    """The chosen coordination structure for a mission's execution."""
    kind: str = "single_agent"
    workers: int = 1
    merge_policy: str = "verify_then_commit"   # verify_then_commit | majority_vote | sequential
    id: str = field(default_factory=lambda: new_id("topo"))


@dataclass
class TaskShape:
    """What the planner knows about a mission's structure — drives the selection."""
    decomposable: float = 0.5     # 0..1: how independently the work splits into parallel subtasks
    coupling: float = 0.5         # 0..1: how sequentially coupled / error-propagating the work is
    subtasks: int = 1
    needs_verification: bool = True


@dataclass
class TopologyProjection:
    """A scored candidate — the output shape an optimizer overlay produces (public deterministic path
    fills only kind/workers/score)."""
    kind: str
    workers: int
    quality: float = 0.0
    latency: float = 0.0
    cost: float = 0.0
    coordination_overhead: float = 0.0
    verification_cost: float = 0.0
    historical_success: float = 0.0
    score: float = 0.0

    def topology(self) -> ExecutionTopology:
        merge = "majority_vote" if self.kind == "voting" else \
                "sequential" if self.kind == "pipeline" else "verify_then_commit"
        return ExecutionTopology(kind=self.kind, workers=self.workers, merge_policy=merge)


@dataclass
class TopologyWeights:
    """Cost-model weights — config the (enterprise) optimizer consumes; harmless/unused in the public
    deterministic path."""
    quality: float = 10.0
    latency: float = 0.6
    cost: float = 0.5
    coordination: float = 2.0
    verification: float = 0.8
    history: float = 4.0


def validate_topology(t: ExecutionTopology) -> list[str]:
    """Structural validation (empty = valid)."""
    errs = []
    if t.kind not in TOPOLOGY_KINDS:
        errs.append(f"unknown topology kind {t.kind!r}")
    if t.workers < 1:
        errs.append("workers must be >= 1")
    if t.merge_policy not in MERGE_POLICIES:
        errs.append(f"unknown merge_policy {t.merge_policy!r}")
    return errs


def rule_choice(shape: TaskShape, max_workers: int = 4) -> ExecutionTopology:
    """Deterministic topology selection from the task shape — the public default (no cost model, no
    learner). Encodes the empirical rule: parallel on decomposable/low-coupling work; single agent (or a
    pipeline for many sequential steps) on coupled work."""
    w = min(max_workers, max(2, shape.subtasks))
    if shape.coupling >= 0.6:                                   # coupled / sequential
        if shape.subtasks >= 3:
            return ExecutionTopology("pipeline", 1, "sequential")
        return ExecutionTopology("single_agent", 1, "verify_then_commit")
    if shape.decomposable >= 0.6:                              # decomposable, low coupling → parallelize
        return ExecutionTopology("coordinator_workers", w, "verify_then_commit")
    if shape.needs_verification and shape.decomposable >= 0.4:  # ensemble, agreement-verified
        return ExecutionTopology("voting", 3, "majority_vote")
    return ExecutionTopology("single_agent", 1, "verify_then_commit")


class TopologyOptimizer(Protocol):
    """The overlay seam. An enterprise optimizer (cost-model scoring + learned per-class success prior)
    implements this and registers it; the public runtime ships none, so selection is deterministic."""

    def select(self, mission_class: str, shape: TaskShape) -> "tuple[ExecutionTopology, list[TopologyProjection]]": ...


_DEFAULT_OPTIMIZER: "TopologyOptimizer | None" = None


def set_default_optimizer(opt: "TopologyOptimizer | None") -> None:
    """Register the process-wide topology optimizer (enterprise overlay). None restores determinism."""
    global _DEFAULT_OPTIMIZER
    _DEFAULT_OPTIMIZER = opt


def get_default_optimizer() -> "TopologyOptimizer | None":
    return _DEFAULT_OPTIMIZER


class TopologyPlanner:
    """Selects the execution topology. Deterministic by default (`rule_choice`); if an optimizer overlay
    is registered it delegates to it (cost-based + learned)."""

    def __init__(self, *, optimizer: "TopologyOptimizer | None" = None, max_workers: int = 4) -> None:
        self.optimizer = optimizer if optimizer is not None else get_default_optimizer()
        self.max_workers = max_workers

    def select(self, mission_class: str, shape: TaskShape) -> "tuple[ExecutionTopology, list[TopologyProjection]]":
        if self.optimizer is not None:
            return self.optimizer.select(mission_class, shape)
        topo = rule_choice(shape, self.max_workers)
        return topo, [TopologyProjection(kind=topo.kind, workers=topo.workers, score=1.0,
                                         historical_success=0.5)]

    def explain(self, mission_class: str, shape: TaskShape) -> dict:
        topo, ranked = self.select(mission_class, shape)
        return {"mission_class": mission_class, "chosen": topo.kind, "workers": topo.workers,
                "policy": "optimizer" if self.optimizer is not None else "deterministic(rule)",
                "ranked": [{"kind": p.kind, "workers": p.workers, "score": p.score} for p in ranked]}


def _demo() -> int:
    checks: list[tuple[str, bool]] = []
    P = TopologyPlanner()
    coupled = P.select("incident", TaskShape(decomposable=0.2, coupling=0.9, subtasks=1))[0]
    checks.append(("coupled work -> single_agent", coupled.kind == "single_agent"))
    seq = P.select("pipeline_job", TaskShape(decomposable=0.3, coupling=0.8, subtasks=5))[0]
    checks.append(("coupled + many subtasks -> pipeline", seq.kind == "pipeline"))
    par = P.select("survey", TaskShape(decomposable=0.9, coupling=0.2, subtasks=6))[0]
    checks.append(("decomposable low-coupling -> coordinator_workers", par.kind == "coordinator_workers"))
    checks.append(("validation catches a bad kind", validate_topology(ExecutionTopology("swarm")) != []))
    checks.append(("a valid topology validates clean", validate_topology(par) == []))
    # overlay: register a fake optimizer that always picks voting; confirm delegation, then restore
    class FakeOpt:
        def select(self, mc, shape): t = ExecutionTopology("voting", 3, "majority_vote"); return t, [TopologyProjection("voting", 3, score=9.9)]
    set_default_optimizer(FakeOpt())
    checks.append(("registered optimizer overrides the deterministic default", TopologyPlanner().select("x", TaskShape())[0].kind == "voting"))
    set_default_optimizer(None)
    checks.append(("optimizer removed -> deterministic again", TopologyPlanner().select("incident", TaskShape(coupling=0.9))[0].kind == "single_agent"))
    for name, ok in checks:
        print(f"  check {name} = {ok}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"RESULT {passed}/{len(checks)}  (topology contract {CONTRACT_VERSION})")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_demo())
