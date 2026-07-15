"""P9a — Active plan selection: simulation as a first-class planning component.

    Candidate plans → policy pruning → simulation → cost/risk/success scoring → plan selection

Exploration is **bounded on purpose** — max candidate count, admissible (policy-scoped) providers
only, a one-swap neighbourhood instead of the full cross-product, and a switch margin the winner
must beat before the runtime abandons the default plan. Otherwise candidate generation blows up
combinatorially and non-determinism creeps back in.

The registry handed in is already policy-scoped, so every candidate is admissible by construction;
a binding the compiler still rejects (fail-closed) is simply dropped from the candidate set.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import cost as costmod
from .compiler import compile_intent, CompileError
from .context import LocalContextRuntime
from .registry import CapabilityRegistry
from .scheduler import SchedulePolicy
from .simulator import simulate
from .types import ExecutionIntent, ExecutionPlan, Mission, SimResult


@dataclass
class PlanCandidate:
    label: str
    prefer: dict           # binding overrides {outcome: capability_name} ({} = the default plan)
    plan: ExecutionPlan
    projection: SimResult
    score: float
    is_default: bool = False
    selected: bool = False


def score_projection(proj: SimResult) -> float:
    """Cost/risk/success into one comparable number (higher = better). Over-budget is inadmissible."""
    if not proj.within_budget:
        return float("-inf")
    return proj.expected_success * 10.0 - proj.expected_usd - 0.5 * proj.expected_approvals


class ActivePlanner:
    def __init__(self, registry: CapabilityRegistry, policy: SchedulePolicy, learning=None, *,
                 max_candidates: int = 6, switch_margin: float = 0.05):
        self.registry = registry
        self.context = LocalContextRuntime(registry)   # binding resolves through the Context Runtime
        self.policy = policy
        self.learning = learning
        self.max_candidates = max(1, max_candidates)
        self.switch_margin = switch_margin

    def select(self, mission: Mission, intent: ExecutionIntent, *, revision: int = 1,
               reason: str = "initial") -> tuple[ExecutionPlan, list[PlanCandidate]]:
        cands: list[PlanCandidate] = []
        for label, pref in self._candidate_prefers(intent):
            try:
                plan = compile_intent(mission, intent, self.context,
                                      revision=revision, reason=reason, prefer=pref)
            except CompileError:
                continue                              # policy pruning: an inadmissible binding is dropped
            proj = simulate(plan, mission, self.registry, self.policy)
            cands.append(PlanCandidate(label=label, prefer=pref, plan=plan, projection=proj,
                                       score=score_projection(proj), is_default=(pref == {})))
        if not cands:
            # no admissible candidate — recompile the default so the caller gets the real CompileError
            plan = compile_intent(mission, intent, self.context, revision=revision, reason=reason)
            proj = simulate(plan, mission, self.registry, self.policy)
            cands = [PlanCandidate("default", {}, plan, proj, score_projection(proj), is_default=True)]

        default = next((c for c in cands if c.is_default), cands[0])
        best = max(cands, key=lambda c: c.score)
        # bounded: only abandon the default when a candidate clearly wins (confidence threshold)
        chosen = best if best.score > default.score + self.switch_margin else default
        chosen.selected = True
        chosen.plan.projection = chosen.projection
        return chosen.plan, cands

    def _candidate_prefers(self, intent: ExecutionIntent):
        """The default plan first, then a bounded one-swap neighbourhood: each alternative swaps a
        single outcome to a non-default admissible provider. One-swap, not the cross-product."""
        prefers: list[tuple[str, dict]] = [("default", {})]
        for step in intent.steps:
            ranked = costmod.rank_candidates(self.registry.providing(step.outcome))
            for alt in ranked[1:]:                    # skip the default (rank-best) provider
                prefers.append((f"{step.outcome}={alt.name}", {step.outcome: alt.name}))
                if len(prefers) >= self.max_candidates:
                    return prefers
        return prefers
