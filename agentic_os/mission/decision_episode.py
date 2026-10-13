"""Immutable decision episode contract for benchmark learning/evaluation.

An episode ties a bounded decision request/result to the action actually taken, independent readback,
delayed outcome and reward. It is intentionally small: benchmark runners can persist this object without
depending on a concrete app core, model provider or learning algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Mapping

from agentic_os.mission.apps_e2e import OracleVerdict
from agentic_os.mission.context_view import content_hash

CONTRACT_VERSION = "decision-episode/v1"


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _after(left: str, right: str) -> bool:
    """Return True when ISO-8601-ish timestamps are out of order.

    The contract expects normalized UTC timestamps, so lexical comparison is deterministic and keeps this
    dependency-free for runtimes and fixtures.
    """

    return bool(left and right and left > right)


@dataclass(frozen=True)
class DecisionEpisode:
    episode_id: str
    scenario_id: str
    decision_id: str
    tenant_id: str
    request_digest: str
    result_digest: str
    eligible_options: tuple[str, ...]
    selected_option: str | None
    abstained: bool
    behavior_policy_id: str
    behavior_policy_pin: str
    as_of: str
    known_at: str
    decision_time: str
    propensity: float | None = None
    policy_seed_digest: str = ""
    approval_ref: str = ""
    execution_ref: str = ""
    execution_time: str = ""
    readback_ref: str = ""
    observation_time: str = ""
    evaluation_cutoff: str = ""
    oracle_verdict: OracleVerdict | str = OracleVerdict.UNRESOLVED
    outcome_ref: str = ""
    reward: Mapping[str, float] = field(default_factory=dict)
    censored: bool = False
    censor_reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in (
            "episode_id", "scenario_id", "decision_id", "tenant_id", "request_digest", "result_digest",
            "behavior_policy_id", "behavior_policy_pin", "as_of", "known_at", "decision_time",
        ):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        if not self.eligible_options:
            errors.append("eligible_options is required")
        if len(set(self.eligible_options)) != len(self.eligible_options):
            errors.append("eligible_options contains duplicates")
        if self.abstained and self.selected_option is not None:
            errors.append("abstained episode must not select an option")
        if not self.abstained and self.selected_option not in self.eligible_options:
            errors.append("selected_option must be one of eligible_options")
        if self.propensity is not None:
            if not isinstance(self.propensity, (int, float)) or not math.isfinite(float(self.propensity)):
                errors.append("propensity must be finite")
            elif float(self.propensity) <= 0.0 or float(self.propensity) > 1.0:
                errors.append("propensity must be in (0, 1]")
        try:
            verdict = OracleVerdict(_enum_value(self.oracle_verdict))
        except ValueError:
            errors.append(f"unknown oracle verdict {self.oracle_verdict!r}")
            verdict = None
        if self.censored and not self.censor_reason:
            errors.append("censor_reason is required when censored is true")
        if verdict in (
            OracleVerdict.VERIFIED_SUCCESS,
            OracleVerdict.CORRECT_HOLD,
            OracleVerdict.VERIFIED_FAILURE,
        ):
            if not self.outcome_ref:
                errors.append("outcome_ref is required for observed oracle verdicts")
            if not self.observation_time:
                errors.append("observation_time is required for observed oracle verdicts")
        for name, value in self.reward.items():
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                errors.append(f"reward value for {name!r} must be finite")
        if _after(self.as_of, self.known_at):
            errors.append("as_of must not be after known_at")
        if _after(self.known_at, self.decision_time):
            errors.append("known_at must not be after decision_time")
        if _after(self.decision_time, self.execution_time):
            errors.append("decision_time must not be after execution_time")
        if _after(self.execution_time, self.observation_time):
            errors.append("execution_time must not be after observation_time")
        if _after(self.decision_time, self.evaluation_cutoff):
            errors.append("decision_time must not be after evaluation_cutoff")
        return tuple(errors)

    def is_valid(self) -> bool:
        return not self.validation_errors()

    def has_action_support(self) -> bool:
        return (
            not self.abstained
            and self.selected_option in self.eligible_options
            and self.propensity is not None
            and math.isfinite(float(self.propensity))
            and 0.0 < float(self.propensity) <= 1.0
        )

    def has_observed_outcome(self) -> bool:
        try:
            verdict = OracleVerdict(_enum_value(self.oracle_verdict))
        except ValueError:
            return False
        return (
            verdict
            in (OracleVerdict.VERIFIED_SUCCESS, OracleVerdict.CORRECT_HOLD, OracleVerdict.VERIFIED_FAILURE)
            and bool(self.readback_ref)
            and bool(self.outcome_ref)
            and bool(self.observation_time)
            and not self.censored
        )

    def learning_eligible(self) -> bool:
        return self.is_valid() and self.has_action_support() and self.has_observed_outcome()

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "episode_id": self.episode_id,
            "scenario_id": self.scenario_id,
            "decision_id": self.decision_id,
            "tenant_id": self.tenant_id,
            "request_digest": self.request_digest,
            "result_digest": self.result_digest,
            "eligible_options": list(self.eligible_options),
            "selected_option": self.selected_option,
            "abstained": self.abstained,
            "behavior_policy_id": self.behavior_policy_id,
            "behavior_policy_pin": self.behavior_policy_pin,
            "propensity": self.propensity,
            "policy_seed_digest": self.policy_seed_digest,
            "approval_ref": self.approval_ref,
            "execution_ref": self.execution_ref,
            "readback_ref": self.readback_ref,
            "oracle_verdict": _enum_value(self.oracle_verdict),
            "outcome_ref": self.outcome_ref,
            "reward": dict(sorted((k, float(v)) for k, v in self.reward.items())),
            "censored": self.censored,
            "censor_reason": self.censor_reason,
            "as_of": self.as_of,
            "known_at": self.known_at,
            "decision_time": self.decision_time,
            "execution_time": self.execution_time,
            "observation_time": self.observation_time,
            "evaluation_cutoff": self.evaluation_cutoff,
            "metadata": dict(sorted(self.metadata.items())),
        }

    def digest(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


__all__ = [
    "CONTRACT_VERSION",
    "DecisionEpisode",
]
