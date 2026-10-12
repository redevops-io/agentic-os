"""Apps E2E benchmark runner contract.

This adapter connects the Apps E2E scenario vocabulary to the existing Evaluation Runtime without
standing up real app cores. Concrete harnesses inject the app executor/oracle; the runtime-owned part
records decision-provider output, independent readback/outcome evidence and learning eligibility as
versioned decision episodes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
from typing import Callable, Mapping

from agentic_os.mission.apps_e2e import AppsE2EScenario, CoverageMatrix, OracleVerdict, coverage_matrix
from agentic_os.mission.context_view import content_hash
from agentic_os.mission.decision_episode import DecisionEpisode
from agentic_os.mission.decision_provider import DecisionProvider, DecisionRequest, DecisionResult, validate_decision
from agentic_os.mission.evaluation import Dataset, EvaluationProgram, EvaluationProtocol, HarnessProfile

CONTRACT_VERSION = "apps-e2e-runner/v1"


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


@dataclass(frozen=True)
class AppExecutionOutcome:
    """Result supplied by the concrete app harness after execution and independent verification."""

    oracle_verdict: OracleVerdict | str
    execution_ref: str = ""
    readback_ref: str = ""
    outcome_ref: str = ""
    reward: Mapping[str, float] = field(default_factory=dict)
    execution_time: str = ""
    observation_time: str = ""
    evaluation_cutoff: str = ""
    censored: bool = False
    censor_reason: str = ""


@dataclass(frozen=True)
class ScenarioRun:
    scenario_id: str
    scenario_digest: str
    request_digest: str
    result_digest: str
    episode: DecisionEpisode


@dataclass(frozen=True)
class AppsE2EBenchmarkReport:
    suite_id: str
    suite_version: str
    coverage: CoverageMatrix
    runs: tuple[ScenarioRun, ...]
    metrics: Mapping[str, float]
    verdict_counts: Mapping[str, int]

    @property
    def complete(self) -> bool:
        return self.coverage.complete

    @property
    def publishable(self) -> bool:
        return self.coverage.complete and bool(self.runs) and all(run.episode.has_observed_outcome()
                                                                  for run in self.runs)

    def digest(self) -> str:
        payload = {
            "contract_version": CONTRACT_VERSION,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "coverage_complete": self.coverage.complete,
            "run_digests": [run.episode.digest() for run in self.runs],
            "metrics": dict(sorted((k, float(v)) for k, v in self.metrics.items())),
            "verdict_counts": dict(sorted(self.verdict_counts.items())),
        }
        return content_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _suite_digest(scenarios: tuple[AppsE2EScenario, ...]) -> str:
    return content_hash(json.dumps([s.digest() for s in scenarios], sort_keys=True, separators=(",", ":")))


def evaluation_program_for_suite(
    suite_id: str,
    suite_version: str,
    scenarios: tuple[AppsE2EScenario, ...],
    *,
    metrics: tuple[str, ...] = (
        "verified_success_rate",
        "correct_hold_rate",
        "verified_failure_rate",
        "learning_eligible_rate",
        "coverage_complete",
    ),
    harness: HarnessProfile = HarnessProfile.VERIFICATION_HEAVY,
) -> EvaluationProgram:
    """Create a pinned Evaluation Runtime program for an Apps E2E suite."""

    for scenario in scenarios:
        errors = scenario.validation_errors()
        if errors:
            raise ValueError("; ".join(errors))
    suite_hash = _suite_digest(scenarios)
    dataset = Dataset(
        id=f"{suite_id}-scenarios",
        version=suite_version,
        content_hash=suite_hash,
        provenance=CONTRACT_VERSION,
        limitations="Scenario manifests only; concrete app harness supplies execution evidence.",
        compatibility_hash=suite_hash,
    )
    protocol = EvaluationProtocol(
        id="apps-e2e-independent-verification",
        version="1",
        metrics=metrics,
        verification_policy="independent-readback-required",
        statistical_procedure="paired lanes; decision episodes require behavior-policy propensity for learning",
        execution_lag="scenario-defined",
        cost_model="reported by concrete harness",
    )
    return EvaluationProgram(suite_id, suite_version, dataset, protocol, harness=harness,
                             steps=tuple(s.id for s in scenarios))


def run_scenario(
    scenario: AppsE2EScenario,
    request: DecisionRequest,
    provider: DecisionProvider,
    execute: Callable[[AppsE2EScenario, DecisionResult], AppExecutionOutcome],
    *,
    tenant_id: str,
    behavior_policy_id: str,
    behavior_policy_pin: str,
    propensity: float | None,
    decision_time: str,
    policy_seed_digest: str = "",
    approval_ref: str = "",
    metadata: Mapping[str, object] | None = None,
) -> ScenarioRun:
    """Run one scenario through a provider and injected app oracle, returning a decision episode."""

    scenario_errors = scenario.validation_errors()
    if scenario_errors:
        raise ValueError("; ".join(scenario_errors))
    if scenario.decision_lane is not None:
        if scenario.decision_lane.provider_id != provider.provider_id:
            raise ValueError("scenario decision_lane provider_id does not match provider")
        if scenario.decision_lane.model_pin != provider.model_pin:
            raise ValueError("scenario decision_lane model_pin does not match provider")
    result = validate_decision(provider, request)
    outcome = execute(scenario, result)
    episode = DecisionEpisode(
        episode_id=f"ep-{content_hash(scenario.id, result.response_digest, decision_time)}",
        scenario_id=scenario.id,
        decision_id=request.decision_id,
        tenant_id=tenant_id,
        request_digest=request.digest(),
        result_digest=result.response_digest,
        eligible_options=tuple(request.permitted_options),
        selected_option=result.selected_option,
        abstained=result.abstained,
        behavior_policy_id=behavior_policy_id,
        behavior_policy_pin=behavior_policy_pin,
        propensity=propensity,
        policy_seed_digest=policy_seed_digest,
        approval_ref=approval_ref,
        execution_ref=outcome.execution_ref,
        execution_time=outcome.execution_time,
        readback_ref=outcome.readback_ref,
        observation_time=outcome.observation_time,
        evaluation_cutoff=outcome.evaluation_cutoff,
        oracle_verdict=outcome.oracle_verdict,
        outcome_ref=outcome.outcome_ref,
        reward=outcome.reward,
        censored=outcome.censored,
        censor_reason=outcome.censor_reason,
        as_of=request.as_of,
        known_at=request.known_at,
        decision_time=decision_time,
        metadata=dict(metadata or {}),
    )
    episode_errors = episode.validation_errors()
    if episode_errors:
        raise ValueError("; ".join(episode_errors))
    return ScenarioRun(
        scenario_id=scenario.id,
        scenario_digest=scenario.digest(),
        request_digest=request.digest(),
        result_digest=result.response_digest,
        episode=episode,
    )


def summarize_runs(
    suite_id: str,
    suite_version: str,
    scenarios: tuple[AppsE2EScenario, ...],
    runs: tuple[ScenarioRun, ...],
    *,
    unsupported: Mapping[str, str] | None = None,
) -> AppsE2EBenchmarkReport:
    """Produce publishable benchmark metrics without hiding unsupported or unresolved cases."""

    coverage = coverage_matrix(scenarios, unsupported=unsupported)
    total = len(runs)
    counts = Counter(_enum_value(run.episode.oracle_verdict) for run in runs)
    observed = sum(1 for run in runs if run.episode.has_observed_outcome())
    learning_eligible = sum(1 for run in runs if run.episode.learning_eligible())
    denominator = float(total or 1)
    metrics = {
        "coverage_complete": 1.0 if coverage.complete else 0.0,
        "observed_rate": observed / denominator,
        "learning_eligible_rate": learning_eligible / denominator,
        "verified_success_rate": counts[OracleVerdict.VERIFIED_SUCCESS.value] / denominator,
        "correct_hold_rate": counts[OracleVerdict.CORRECT_HOLD.value] / denominator,
        "verified_failure_rate": counts[OracleVerdict.VERIFIED_FAILURE.value] / denominator,
        "unresolved_rate": counts[OracleVerdict.UNRESOLVED.value] / denominator,
        "unsupported_rate": counts[OracleVerdict.UNSUPPORTED.value] / denominator,
    }
    return AppsE2EBenchmarkReport(suite_id, suite_version, coverage, runs, metrics, dict(counts))


__all__ = [
    "CONTRACT_VERSION",
    "AppExecutionOutcome",
    "AppsE2EBenchmarkReport",
    "ScenarioRun",
    "evaluation_program_for_suite",
    "run_scenario",
    "summarize_runs",
]
