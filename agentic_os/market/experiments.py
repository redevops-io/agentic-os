"""Experiment planning (Market-Intelligence plan §5, §11, Phase 5).

Turn ranked :class:`Opportunity` objects into governed :class:`Experiment` objects: pick the best few within a
capacity, attach an EXPLAIN grounded in the evidence, estimate cost/reversibility, and — crucially — return an
explicit ``no_change`` decision for opportunities that don't clear the bar (a valid outcome, not a gap in the
output). The concrete change text can be refined by an injectable ``ChangeWriter`` (a Context-Runtime / LLM
step); the deterministic default is grounded in the opportunity. Nothing here executes.
"""
from __future__ import annotations

from typing import List, Optional, Protocol, Sequence, Tuple

from .contracts import Experiment, Opportunity, Provenance


class ChangeWriter(Protocol):
    """Optional refinement of an opportunity into a concrete change description (e.g. a Context-Runtime call).
    Must return a plain change string; the planner keeps the opportunity's evidence + value regardless."""
    def write(self, opportunity: Opportunity) -> str: ...


def _explain(o: Opportunity, decision: str) -> str:
    n = len(o.evidence_refs)
    why = (f"{o.gap}. Expected value {o.expected_value} (competitor prevalence), {o.reversibility}, "
           f"{n} supporting observation(s).")
    return (f"PROPOSE: {why}" if decision == "proposed"
            else f"NO_CHANGE: below the bar — {why}")


def plan_experiments(opportunities: Sequence[Opportunity], *, capacity: int = 3,
                     min_expected_value: float = 0.34, writer: Optional[ChangeWriter] = None
                     ) -> Tuple[Experiment, ...]:
    """Rank opportunities by expected value; the top `capacity` that clear `min_expected_value` become
    ``proposed`` experiments, the rest ``no_change``. Deterministic; every opportunity yields exactly one
    Experiment so the full decision set is auditable."""
    ranked = sorted(opportunities, key=lambda o: o.expected_value, reverse=True)
    out: List[Experiment] = []
    proposed = 0
    for o in ranked:
        qualifies = o.expected_value >= min_expected_value and proposed < capacity
        decision = "proposed" if qualifies else "no_change"
        if qualifies:
            proposed += 1
        change = (writer.write(o) if (writer is not None and qualifies) else o.proposed_experiment)
        out.append(Experiment(
            prov=Provenance("internal.experiment_planner", f"{o.site}:{o.pattern_ref}"),
            opportunity_ref=o.prov.provider_ref, site=o.site,
            hypothesis=f"Adopting '{o.pattern_ref}' improves acquisition on {o.site}",
            change=change, decision=decision, cost=0.0, reversibility=o.reversibility,
            expected_value=o.expected_value, confidence=o.confidence,
            explain=_explain(o, decision), evidence_refs=o.evidence_refs))
    return tuple(out)


def proposed(experiments: Sequence[Experiment]) -> Tuple[Experiment, ...]:
    """The subset actually put forward for approval (decision == 'proposed')."""
    return tuple(e for e in experiments if e.decision == "proposed")
