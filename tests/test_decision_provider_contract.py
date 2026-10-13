import math

import pytest

from agentic_os.mission.apps_e2e import ScoreSemantics
from agentic_os.mission.decision_provider import (
    DecisionRequest,
    DecisionResult,
    StaticDecisionProvider,
    validate_decision,
)


def _request(**kwargs) -> DecisionRequest:
    base = dict(
        decision_id="dec-1",
        tenant_id="tenant-a",
        purpose="insurance_renewal_triage",
        question_kind="choice",
        evidence_view_ref="cv-insurance-1",
        permitted_options=("HOLD", "FOLLOW_UP", "COLLECT"),
        constraints_ref="policy@sha256:abc",
        as_of="2026-01-01T00:00:00Z",
        known_at="2026-01-01T00:00:00Z",
        latency_budget_ms=50,
        data_classification="synthetic",
    )
    base.update(kwargs)
    return DecisionRequest(**base)


def test_static_provider_returns_valid_pinned_decision():
    req = _request()
    provider = StaticDecisionProvider("static", "static@1")
    result = validate_decision(provider, req)
    assert result.selected_option == "HOLD"
    assert result.input_digest == req.digest()
    assert result.response_digest
    assert result.is_valid_for(req)


def test_request_validation_rejects_missing_and_duplicate_contract_fields():
    req = _request(decision_id="", permitted_options=("HOLD", "HOLD"), latency_budget_ms=0)
    assert req.validation_errors() == (
        "decision_id is required",
        "permitted_options contains duplicates",
        "latency_budget_ms must be positive",
    )


def test_result_rejects_unknown_selected_option_and_scores():
    req = _request()
    result = DecisionResult(
        decision_id=req.decision_id,
        provider_id="bad",
        model_pin="bad@1",
        output_kind="choice",
        option_scores={"DELETE_ALL": 0.9},
        score_semantics=ScoreSemantics.RANK_SCORE,
        selected_option="DELETE_ALL",
        abstained=False,
        input_digest=req.digest(),
    )
    assert result.validation_errors(req) == (
        "selected_option must be one of permitted_options",
        "option_scores contains unknown options: DELETE_ALL",
    )


def test_result_rejects_nonfinite_scores_and_uncertainty():
    req = _request()
    result = DecisionResult(
        decision_id=req.decision_id,
        provider_id="bad",
        model_pin="bad@1",
        output_kind="choice",
        option_scores={"HOLD": math.nan},
        score_semantics=ScoreSemantics.RANK_SCORE,
        selected_option="HOLD",
        abstained=False,
        input_digest=req.digest(),
        uncertainty={"entropy": math.inf},
    )
    assert result.validation_errors(req) == (
        "score for 'HOLD' must be finite",
        "uncertainty value for 'entropy' must be finite",
    )


def test_class_probability_scores_must_be_distribution():
    req = _request(permitted_options=("HOLD", "FOLLOW_UP"))
    result = DecisionResult(
        decision_id=req.decision_id,
        provider_id="prob",
        model_pin="prob@1",
        output_kind="choice",
        option_scores={"HOLD": 0.6, "FOLLOW_UP": 0.6},
        score_semantics=ScoreSemantics.CLASS_PROB,
        selected_option="HOLD",
        abstained=False,
        input_digest=req.digest(),
    )
    assert result.validation_errors(req) == ("class_prob option_scores must sum to 1.0",)


def test_outcome_probability_scores_are_bounded_but_not_normalized():
    req = _request(permitted_options=("HOLD", "FOLLOW_UP"))
    result = DecisionResult(
        decision_id=req.decision_id,
        provider_id="prob",
        model_pin="prob@1",
        output_kind="score",
        option_scores={"HOLD": 0.8, "FOLLOW_UP": 0.7},
        score_semantics=ScoreSemantics.OUTCOME_PROB,
        selected_option="HOLD",
        abstained=False,
        input_digest=req.digest(),
    )
    assert result.validation_errors(req) == ()


def test_abstention_must_be_allowed_and_select_nothing():
    req = _request(abstain_allowed=False)
    result = DecisionResult(
        decision_id=req.decision_id,
        provider_id="abstain",
        model_pin="abstain@1",
        output_kind="choice",
        option_scores={},
        score_semantics=ScoreSemantics.RANK_SCORE,
        selected_option="HOLD",
        abstained=True,
        input_digest=req.digest(),
    )
    assert result.validation_errors(req) == (
        "abstained result must not select an option",
        "request does not allow abstention",
    )


def test_validate_decision_fails_closed_on_provider_contract_violation():
    req = _request()

    class BadProvider:
        provider_id = "bad"
        model_pin = "bad@1"

        def decide(self, request):
            return DecisionResult(
                decision_id=request.decision_id,
                provider_id=self.provider_id,
                model_pin=self.model_pin,
                output_kind="choice",
                option_scores={"HOLD": 1.0},
                score_semantics="nonsense",
                selected_option="HOLD",
                abstained=False,
                input_digest=request.digest(),
            )

    with pytest.raises(ValueError, match="unknown score_semantics"):
        validate_decision(BadProvider(), req)
