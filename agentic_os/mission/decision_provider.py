"""Typed DecisionProvider contract for bounded decision models.

Decision providers are proposal engines: deterministic rules, small decision models, or LLM-backed
classifiers can score a fixed option set, but they never receive write authority. Mission/Gateway policy,
approval and independent readback remain the execution boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Callable, Mapping, Protocol

from agentic_os.mission.apps_e2e import ScoreSemantics
from agentic_os.mission.context_view import content_hash

CONTRACT_VERSION = "decision-provider/v1"
_TOL = 1e-6


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


@dataclass(frozen=True)
class DecisionRequest:
    decision_id: str
    tenant_id: str
    purpose: str
    question_kind: str               # predicate | choice | score | rubric
    evidence_view_ref: str
    permitted_options: tuple[str, ...]
    constraints_ref: str
    as_of: str
    known_at: str
    abstain_allowed: bool = True
    latency_budget_ms: int = 100
    max_cost: str = ""
    data_classification: str = "synthetic"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in ("decision_id", "tenant_id", "purpose", "question_kind",
                     "evidence_view_ref", "constraints_ref", "as_of", "known_at"):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        if not self.permitted_options:
            errors.append("permitted_options is required")
        if len(set(self.permitted_options)) != len(self.permitted_options):
            errors.append("permitted_options contains duplicates")
        if self.latency_budget_ms <= 0:
            errors.append("latency_budget_ms must be positive")
        if not self.data_classification:
            errors.append("data_classification is required")
        return tuple(errors)

    def digest(self) -> str:
        payload = {
            "contract_version": CONTRACT_VERSION,
            "decision_id": self.decision_id,
            "tenant_id": self.tenant_id,
            "purpose": self.purpose,
            "question_kind": self.question_kind,
            "evidence_view_ref": self.evidence_view_ref,
            "permitted_options": list(self.permitted_options),
            "constraints_ref": self.constraints_ref,
            "as_of": self.as_of,
            "known_at": self.known_at,
            "abstain_allowed": self.abstain_allowed,
            "latency_budget_ms": self.latency_budget_ms,
            "max_cost": self.max_cost,
            "data_classification": self.data_classification,
            "metadata": dict(sorted(self.metadata.items())),
        }
        return content_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    provider_id: str
    model_pin: str
    output_kind: str
    option_scores: Mapping[str, float]
    score_semantics: ScoreSemantics | str
    selected_option: str | None
    abstained: bool
    input_digest: str
    response_digest: str = ""
    calibration_pin: str = ""
    uncertainty: Mapping[str, float] = field(default_factory=dict)
    usage: Mapping[str, object] = field(default_factory=dict)

    def validation_errors(self, request: DecisionRequest) -> tuple[str, ...]:
        errors = list(request.validation_errors())
        if self.decision_id != request.decision_id:
            errors.append("decision_id does not match request")
        if self.input_digest != request.digest():
            errors.append("input_digest does not match request")
        if not self.provider_id:
            errors.append("provider_id is required")
        if not self.model_pin:
            errors.append("model_pin is required")
        try:
            semantics = ScoreSemantics(_enum_value(self.score_semantics))
        except ValueError:
            errors.append(f"unknown score_semantics {self.score_semantics!r}")
            semantics = None
        if self.abstained and self.selected_option is not None:
            errors.append("abstained result must not select an option")
        if self.abstained and not request.abstain_allowed:
            errors.append("request does not allow abstention")
        if not self.abstained and self.selected_option not in request.permitted_options:
            errors.append("selected_option must be one of permitted_options")
        unknown = sorted(set(self.option_scores) - set(request.permitted_options))
        if unknown:
            errors.append(f"option_scores contains unknown options: {', '.join(unknown)}")
        for option, score in self.option_scores.items():
            if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                errors.append(f"score for {option!r} must be finite")
        for key, value in self.uncertainty.items():
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                errors.append(f"uncertainty value for {key!r} must be finite")
        if semantics in (ScoreSemantics.CLASS_PROB, ScoreSemantics.OUTCOME_PROB):
            for option, score in self.option_scores.items():
                if float(score) < 0.0 or float(score) > 1.0:
                    errors.append(f"probability score for {option!r} must be in [0, 1]")
        if semantics is ScoreSemantics.CLASS_PROB and self.option_scores:
            total = sum(float(v) for v in self.option_scores.values())
            if abs(total - 1.0) > _TOL:
                errors.append("class_prob option_scores must sum to 1.0")
        return tuple(errors)

    def is_valid_for(self, request: DecisionRequest) -> bool:
        return not self.validation_errors(request)

    def with_response_digest(self) -> "DecisionResult":
        payload = {
            "decision_id": self.decision_id,
            "provider_id": self.provider_id,
            "model_pin": self.model_pin,
            "output_kind": self.output_kind,
            "option_scores": dict(sorted((k, float(v)) for k, v in self.option_scores.items())),
            "score_semantics": _enum_value(self.score_semantics),
            "selected_option": self.selected_option,
            "abstained": self.abstained,
            "input_digest": self.input_digest,
            "calibration_pin": self.calibration_pin,
            "uncertainty": dict(sorted((k, float(v)) for k, v in self.uncertainty.items())),
            "usage": dict(sorted(self.usage.items())),
        }
        digest = content_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return DecisionResult(
            decision_id=self.decision_id,
            provider_id=self.provider_id,
            model_pin=self.model_pin,
            output_kind=self.output_kind,
            option_scores=self.option_scores,
            score_semantics=self.score_semantics,
            selected_option=self.selected_option,
            abstained=self.abstained,
            input_digest=self.input_digest,
            response_digest=digest,
            calibration_pin=self.calibration_pin,
            uncertainty=self.uncertainty,
            usage=self.usage,
        )


class DecisionProvider(Protocol):
    provider_id: str
    model_pin: str

    def decide(self, request: DecisionRequest) -> DecisionResult: ...


class StaticDecisionProvider:
    """Deterministic/mock adapter for contract tests and benchmark harness development."""

    def __init__(self, provider_id: str, model_pin: str,
                 scorer: Callable[[DecisionRequest], Mapping[str, float]] | None = None,
                 *, score_semantics: ScoreSemantics = ScoreSemantics.RANK_SCORE) -> None:
        self.provider_id = provider_id
        self.model_pin = model_pin
        self.score_semantics = score_semantics
        self._scorer = scorer

    def decide(self, request: DecisionRequest) -> DecisionResult:
        errors = request.validation_errors()
        if errors:
            raise ValueError("; ".join(errors))
        scores = dict(self._scorer(request) if self._scorer else
                      {option: float(len(request.permitted_options) - idx)
                       for idx, option in enumerate(request.permitted_options)})
        selected = max(scores, key=scores.get) if scores else None
        result = DecisionResult(
            decision_id=request.decision_id,
            provider_id=self.provider_id,
            model_pin=self.model_pin,
            output_kind=request.question_kind,
            option_scores=scores,
            score_semantics=self.score_semantics,
            selected_option=selected,
            abstained=selected is None,
            input_digest=request.digest(),
        ).with_response_digest()
        validation = result.validation_errors(request)
        if validation:
            raise ValueError("; ".join(validation))
        return result


def validate_decision(provider: DecisionProvider, request: DecisionRequest) -> DecisionResult:
    """Run a provider and enforce the contract fail-closed."""

    result = provider.decide(request)
    errors = result.validation_errors(request)
    if errors:
        raise ValueError("; ".join(errors))
    return result if result.response_digest else result.with_response_digest()


__all__ = [
    "CONTRACT_VERSION",
    "DecisionProvider",
    "DecisionRequest",
    "DecisionResult",
    "StaticDecisionProvider",
    "validate_decision",
]
