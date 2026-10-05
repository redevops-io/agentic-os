"""Commercial hypothesis + A/B experiment with a frozen evidence snapshot (Phase 6).

Market evidence (a persistent, cross-competitor tactic) is a HYPOTHESIS about what might work for us — never
proof (plan §6 evidence hierarchy: levels 1-4 generate hypotheses, 5-6 generate knowledge). This turns a
hypothesis into a governed experiment whose success definition and evidence are FROZEN before any result is
observed, so an outcome can never retro-justify the hypothesis. The experiment compiles onto the existing
Mission Runtime for execution; this module owns the design + evaluation, not the running.

  CommercialHypothesis  →  freeze_experiment(success def + evidence)  →  seal  →  (run via Mission)  →  evaluate

``evaluate`` judges the observed outcome ONLY against the frozen definition (expected direction, threshold,
guardrails) — guardrail breaches stop first, and nothing is called a win before the minimum observation window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Dict, Mapping, Optional, Tuple

from runtime_contracts.protocol import content_hash

from ..integrations.business.contracts import BusinessObject

DIRECTIONS = ("up", "down")


@dataclass(frozen=True)
class CommercialHypothesis(BusinessObject):
    """A testable claim derived from market evidence (plan §7). A market change must not jump straight to
    execution — it becomes a hypothesis first, with an expected metric + direction and candidate tests."""
    KIND: ClassVar[str] = "market.commercial_hypothesis"
    observed_pattern: str = ""
    proposed_mechanism: str = ""
    affected_funnel_stage: str = ""
    expected_metric: str = ""
    expected_direction: str = "up"       # up | down
    confidence: float = 0.0
    evidence_refs: Tuple[str, ...] = ()
    candidate_tests: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CommercialExperiment(BusinessObject):
    """A governed A/B experiment. ``evidence_seal`` content-addresses the FROZEN success definition + evidence
    snapshot taken at creation — the single defence against moving the goalposts after seeing results."""
    KIND: ClassVar[str] = "market.commercial_experiment"
    hypothesis_ref: str = ""
    control: str = ""
    treatment: str = ""
    target_surface: str = ""
    target_population: str = ""
    primary_metric: str = ""
    expected_direction: str = "up"
    success_threshold: float = 0.0       # min absolute metric delta in the expected direction to call a win
    secondary_metrics: Tuple[str, ...] = ()
    guardrails: Dict[str, float] = field(default_factory=dict)   # metric → max tolerated adverse move (abs)
    minimum_observation_s: int = 0
    stopping_policy: str = "fixed_horizon"   # fixed_horizon | significance | guardrail
    rollback: str = ""
    evidence_snapshot: Tuple[str, ...] = ()
    evidence_seal: str = ""
    status: str = "designed"             # designed | running | evaluated | rolled_back


def freeze_experiment(hypothesis: CommercialHypothesis, *, control: str, treatment: str, target_surface: str,
                      primary_metric: str, success_threshold: float, target_population: str = "",
                      secondary_metrics: Tuple[str, ...] = (), guardrails: Optional[Mapping[str, float]] = None,
                      minimum_observation_s: int = 0, stopping_policy: str = "fixed_horizon",
                      rollback: str = "") -> CommercialExperiment:
    """Freeze a hypothesis into an experiment: seal the success definition + evidence so results can't move
    the goalposts. The seal is a content hash over exactly the fields evaluation is allowed to read."""
    guardrails = dict(guardrails or {})
    evidence_snapshot = tuple(hypothesis.evidence_refs)
    seal = content_hash({
        "hypothesis": hypothesis.digest(), "control": control, "treatment": treatment,
        "primary_metric": primary_metric, "expected_direction": hypothesis.expected_direction,
        "success_threshold": success_threshold, "guardrails": dict(sorted(guardrails.items())),
        "minimum_observation_s": minimum_observation_s, "evidence": list(evidence_snapshot)})
    return CommercialExperiment(
        prov=hypothesis.prov, hypothesis_ref=hypothesis.digest(), control=control, treatment=treatment,
        target_surface=target_surface, target_population=target_population, primary_metric=primary_metric,
        expected_direction=hypothesis.expected_direction, success_threshold=success_threshold,
        secondary_metrics=secondary_metrics, guardrails=guardrails,
        minimum_observation_s=minimum_observation_s, stopping_policy=stopping_policy, rollback=rollback,
        evidence_snapshot=evidence_snapshot, evidence_seal=seal)


@dataclass(frozen=True)
class ExperimentEvaluation:
    verdict: str                         # IMPROVED | NO_EFFECT | WORSE | GUARDRAIL_STOP | INCONCLUSIVE
    reason: str
    within_guardrails: bool
    met_minimum_observation: bool
    primary_delta: float
    breached_guardrails: Tuple[str, ...] = ()


def _directional(delta: float, direction: str) -> float:
    """Signed progress in the expected direction (positive = good)."""
    return delta if direction == "up" else -delta


def evaluate(experiment: CommercialExperiment, *, primary_delta: float, elapsed_s: int,
             guardrail_values: Optional[Mapping[str, float]] = None) -> ExperimentEvaluation:
    """Judge an outcome ONLY against the frozen definition. Guardrail breach stops first; nothing is a win
    before the minimum observation window; then direction + threshold decide improved/worse/no_effect."""
    gv = dict(guardrail_values or {})
    breached = tuple(sorted(m for m, limit in experiment.guardrails.items()
                            if m in gv and gv[m] <= -abs(limit)))   # adverse move beyond tolerance
    if breached:
        return ExperimentEvaluation("GUARDRAIL_STOP", f"guardrail(s) breached: {list(breached)}",
                                    within_guardrails=False, met_minimum_observation=elapsed_s >= experiment.minimum_observation_s,
                                    primary_delta=primary_delta, breached_guardrails=breached)
    if elapsed_s < experiment.minimum_observation_s:
        return ExperimentEvaluation("INCONCLUSIVE", "minimum observation window not yet met",
                                    within_guardrails=True, met_minimum_observation=False, primary_delta=primary_delta)
    progress = _directional(primary_delta, experiment.expected_direction)
    if progress >= experiment.success_threshold and experiment.success_threshold > 0:
        verdict, reason = "IMPROVED", f"{experiment.primary_metric} moved {experiment.expected_direction} by {abs(primary_delta)}"
    elif progress < 0:
        verdict, reason = "WORSE", f"{experiment.primary_metric} moved against the hypothesis"
    else:
        verdict, reason = "NO_EFFECT", "no material move in the expected direction"
    return ExperimentEvaluation(verdict, reason, within_guardrails=True, met_minimum_observation=True,
                                primary_delta=primary_delta)
