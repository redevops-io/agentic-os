"""Execution Compiler — the PHYSICAL layer (logical plan -> physical plan). NO LLM.

Binds an ExecutionIntent to concrete capabilities (registry + cost model), orders them into an
ExecutionGraph, injects human/permission/guardrail decisions, assigns idempotency keys, and
validates every node's permissions against the mission's grants (FAIL CLOSED). Because it is
deterministic it is cheap, replayable and unit-testable — the whole point of the split. If the
model ever needs to run here, the layering has leaked: push the decision up into the intent.
"""
from __future__ import annotations

import hashlib
import re

from .context import BIND_CAPABILITY, ContextIntent, ContextRuntime
from .types import (
    Mission, ExecutionIntent, ExecutionPlan, ExecutionGraph, Node, NodeCost, CapabilitySpec,
)


def _node_id(mission_id: str, revision: int, outcome: str) -> str:
    """Deterministic node id — so a graph RECOMPILED after a restart correlates with the
    event-sourced execution state (which is keyed by node id). Durability depends on this."""
    slug = re.sub(r"[^a-z0-9]+", "_", outcome.lower()).strip("_")[:32]
    return f"nd_{mission_id.split('_')[-1][:8]}_{revision}_{slug}"


class CompileError(Exception):
    """Raised when an intent cannot be compiled — no capability, or a permission is not granted."""


def _granted(mission: Mission) -> set[str]:
    return set(mission.policy_refs or [])


def _permitted(cap: CapabilitySpec, granted: set[str]) -> bool:
    if "*" in granted:
        return True
    return all(p in granted for p in cap.permissions)


# Capability binding no longer lives here — it is a CONTEXT decision. The compiler states the need and
# the Context Runtime resolves it (which provider, over provides vs embedding discovery). See context.py.


def _idem(mission_id: str, revision: int, outcome: str) -> str:
    return hashlib.sha256(f"{mission_id}:{revision}:{outcome}".encode()).hexdigest()[:16]


def compile_intent(
    mission: Mission,
    intent: ExecutionIntent,
    context: ContextRuntime,
    revision: int = 1,
    reason: str = "initial",
    prefer: dict[str, str] | None = None,
) -> ExecutionPlan:
    if not hasattr(context, "resolve"):
        # tolerate a bare registry (tests / legacy callers) — wrap it in the default Context Runtime,
        # so binding still resolves through the CR door rather than the compiler reaching the registry.
        from .context import LocalContextRuntime
        context = LocalContextRuntime(context)
    granted = _granted(mission)
    prefer = prefer or {}
    graph = ExecutionGraph()
    outcome_to_node: dict[str, str] = {}

    # First pass: bind every step to a capability + node. Binding is a CONTEXT decision — the compiler
    # states the need; the Context Runtime resolves which capability satisfies it.
    step_nodes: list[tuple] = []
    for step in intent.steps:
        cap = context.resolve(ContextIntent(
            kind=BIND_CAPABILITY, need=step.need, outcome=step.outcome,
            prefer=prefer.get(step.outcome), mission=mission)).value
        if cap is None:
            raise CompileError(f"no capability provides outcome '{step.outcome}' (need: {step.need})")
        if not _permitted(cap, granted):
            missing = [p for p in cap.permissions if p not in granted]
            raise CompileError(f"permission denied for '{cap.name}': missing grant(s) {missing}")
        # a node needs human approval if the capability demands it, or a constraint asks for review
        human = cap.approval_required or any("human" in c.lower() or "review" in c.lower()
                                             for c in step.constraints)
        node = Node(
            capability=cap.name, operator=cap.operator, produces=step.outcome,
            approval_required=human, side_effecting=cap.side_effecting, undo=cap.undo,
            idempotency_key=_idem(mission.id, revision, step.outcome),
            cost=NodeCost(usd=cap.cost.usd, latency_ms=cap.cost.latency_ms,
                          tokens=cap.cost.tokens, human_minutes=cap.cost.human_minutes),
            intent_step_id=step.id,
        )
        node.id = _node_id(mission.id, revision, step.outcome)   # deterministic (restart-safe)
        graph.nodes.append(node)
        outcome_to_node[step.outcome] = node.id
        step_nodes.append((step, node))

    # Second pass: wire data dependencies from inputs_from (outcome -> node id).
    for step, node in step_nodes:
        for upstream_outcome in step.inputs_from:
            up = outcome_to_node.get(upstream_outcome)
            if up:
                node.depends_on.append(up)
            # inputs the node will read from world state at run time
            node.inputs[upstream_outcome] = {"$from_world": upstream_outcome}

    _assert_acyclic(graph)
    return ExecutionPlan(mission_id=mission.id, intent_id=intent.id, graph=graph,
                         revision=revision, reason=reason)


def _assert_acyclic(graph: ExecutionGraph) -> None:
    indeg = {n.id: 0 for n in graph.nodes}
    for n in graph.nodes:
        for _ in n.depends_on:
            indeg[n.id] += 1
    ready = [nid for nid, d in indeg.items() if d == 0]
    seen = 0
    adj: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for n in graph.nodes:
        for dep in n.depends_on:
            adj[dep].append(n.id)
    while ready:
        cur = ready.pop()
        seen += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if seen != len(graph.nodes):
        raise CompileError("execution graph has a cycle")
