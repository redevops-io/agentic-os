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
import os
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

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "scenario_id": self.scenario_id,
            "scenario_digest": self.scenario_digest,
            "request_digest": self.request_digest,
            "result_digest": self.result_digest,
            "episode_digest": self.episode.digest(),
            "episode": self.episode.canonical_payload(),
        }

    def digest(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


@dataclass(frozen=True)
class EpisodeAppendReceipt:
    store_id: str
    sequence: int
    run_digest: str
    episode_digest: str


@dataclass(frozen=True)
class EpisodeEvidenceBundle:
    """Immutable summary of an append-only episode store for publication and replay."""

    store_id: str
    contract_version: str
    run_count: int
    run_digests: tuple[str, ...]
    episode_digests: tuple[str, ...]

    def digest(self) -> str:
        payload = {
            "store_id": self.store_id,
            "contract_version": self.contract_version,
            "run_count": self.run_count,
            "run_digests": list(self.run_digests),
            "episode_digests": list(self.episode_digests),
        }
        return content_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))


class InMemoryEpisodeStore:
    """Append-only decision episode store for tests and local benchmark runs."""

    def __init__(self, store_id: str = "memory") -> None:
        self.store_id = store_id
        self._runs: list[ScenarioRun] = []

    def append(self, run: ScenarioRun) -> EpisodeAppendReceipt:
        _validate_run_payload(run.canonical_payload())
        self._runs.append(run)
        return EpisodeAppendReceipt(self.store_id, len(self._runs), run.digest(), run.episode.digest())

    def runs(self) -> tuple[ScenarioRun, ...]:
        return tuple(self._runs)

    def bundle(self) -> EpisodeEvidenceBundle:
        runs = self.runs()
        return EpisodeEvidenceBundle(
            store_id=self.store_id,
            contract_version=CONTRACT_VERSION,
            run_count=len(runs),
            run_digests=tuple(run.digest() for run in runs),
            episode_digests=tuple(run.episode.digest() for run in runs),
        )


class JsonlEpisodeStore(InMemoryEpisodeStore):
    """Append-only JSONL episode store; reopens by validating stored run and episode digests."""

    def __init__(self, path: str, store_id: str = "jsonl") -> None:
        self.path = path
        super().__init__(store_id=store_id)
        if os.path.exists(path):
            with open(path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._runs.append(scenario_run_from_payload(json.loads(line)))

    def append(self, run: ScenarioRun) -> EpisodeAppendReceipt:
        receipt = super().append(run)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(run.canonical_payload(), sort_keys=True) + "\n")
        return receipt


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


def _validate_run_payload(payload: Mapping[str, object]) -> None:
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("scenario run payload contract_version mismatch")
    episode_payload = payload.get("episode")
    if not isinstance(episode_payload, Mapping):
        raise ValueError("scenario run payload missing episode")
    episode = _episode_from_payload(episode_payload)
    episode_digest = payload.get("episode_digest")
    if episode_digest != episode.digest():
        raise ValueError("episode_digest does not match episode payload")
    expected = {
        "scenario_id": episode.scenario_id,
        "request_digest": episode.request_digest,
        "result_digest": episode.result_digest,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"{key} does not match episode payload")
    if episode.validation_errors():
        raise ValueError("; ".join(episode.validation_errors()))


def _episode_from_payload(payload: Mapping[str, object]) -> DecisionEpisode:
    data = dict(payload)
    data.pop("contract_version", None)
    data["eligible_options"] = tuple(data.get("eligible_options", ()))
    data["reward"] = dict(data.get("reward", {}))
    data["metadata"] = dict(data.get("metadata", {}))
    return DecisionEpisode(**data)


def scenario_run_from_payload(payload: Mapping[str, object]) -> ScenarioRun:
    _validate_run_payload(payload)
    episode = _episode_from_payload(payload["episode"])  # type: ignore[index]
    run = ScenarioRun(
        scenario_id=str(payload["scenario_id"]),
        scenario_digest=str(payload["scenario_digest"]),
        request_digest=str(payload["request_digest"]),
        result_digest=str(payload["result_digest"]),
        episode=episode,
    )
    if payload.get("episode_digest") != run.episode.digest():
        raise ValueError("episode_digest does not match reconstructed episode")
    return run


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
    "EpisodeAppendReceipt",
    "EpisodeEvidenceBundle",
    "InMemoryEpisodeStore",
    "JsonlEpisodeStore",
    "ScenarioRun",
    "evaluation_program_for_suite",
    "run_scenario",
    "scenario_run_from_payload",
    "summarize_runs",
]
