import math

from agentic_os.mission.assessment import (
    Assessment,
    ErrataRegister,
    ErratumStatus,
    EvaluationMethodology,
    PublicationErratum,
    PublicationGate,
    PublicationGateStatus,
    comparability_verdict,
    publication_decision_from_gates,
)


def _methodology(**kwargs) -> EvaluationMethodology:
    base = dict(
        methodology_id="apps-e2e-raaal-methodology",
        version="2026.10",
        objective="Measure verified cross-app decisions without claiming unmeasured learning improvement.",
        rules={
            "independent_oracle": "Outcomes come from readback, not mission self-report.",
            "learning_gate": "Policy-improvement claims require support and holdout separation.",
        },
        realization_checks={
            "independent_oracle": "manifest.explainability.independent_verification_rate",
            "learning_gate": "manifest.learning.publication_gate",
        },
        metric_semantics={"VE2ES": "verified end-to-end execution success"},
        decision_model_identity={
            "provider_id": "crossapp-deterministic",
            "model_pin": "crossapp-deterministic@v1",
            "score_semantics": "rank_score",
        },
    )
    base.update(kwargs)
    return EvaluationMethodology(**base)


def _assessment(methodology: EvaluationMethodology, **kwargs) -> Assessment:
    base = dict(
        assessment_id="assessment-crossapp-1",
        suite_id="crossapp-lane-a",
        suite_version="2026.10",
        methodology_id=methodology.methodology_id,
        methodology_hash=methodology.semantic_hash(),
        run_refs=("episodes.jsonl#1", "episodes.jsonl#2"),
        metrics={"VE2ES": 0.5, "correct_hold_rate": 0.5},
        trial_count=2,
        verified_count=2,
    )
    base.update(kwargs)
    return Assessment(**base)


def test_methodology_requires_realization_checks_for_declared_rules():
    methodology = _methodology(realization_checks={"independent_oracle": "manifest.explainability"})

    assert methodology.validation_errors() == (
        "realization_checks missing declared rules: learning_gate",
    )


def test_methodology_hash_includes_decision_model_identity_and_semantics():
    a = _methodology()
    b = _methodology(decision_model_identity={
        "provider_id": "crossapp-deterministic",
        "model_pin": "crossapp-deterministic@v2",
        "score_semantics": "rank_score",
    })

    assert a.validation_errors() == ()
    assert a.semantic_hash() != b.semantic_hash()


def test_assessment_is_facts_only_and_checks_methodology_identity():
    methodology = _methodology()
    assessment = _assessment(methodology, metrics={"VE2ES": 0.5, "publishable": 1.0})

    assert assessment.validation_errors(methodology) == (
        "assessment metrics must not encode publication verdicts: publishable",
    )

    wrong_hash = _assessment(methodology, methodology_hash="sha256:not-this", metrics={"VE2ES": 0.5})
    assert wrong_hash.validation_errors(methodology) == ("methodology_hash does not match methodology",)


def test_assessment_rejects_nonfinite_metrics_and_impossible_counts():
    methodology = _methodology()
    assessment = _assessment(
        methodology,
        metrics={"VE2ES": math.nan},
        trial_count=1,
        verified_count=2,
        hidden_trial_count=-1,
    )

    assert assessment.validation_errors(methodology) == (
        "verified_count must not exceed trial_count",
        "hidden_trial_count must be non-negative",
        "metric 'VE2ES' must be finite",
    )


def test_changed_methodology_hash_invalidates_comparison():
    old = _methodology()
    new = _methodology(rules={
        "independent_oracle": "Outcomes come from readback, not mission self-report.",
        "learning_gate": "Policy-improvement claims require support and holdout separation.",
        "redaction_gate": "Public artifacts must be redaction-clean.",
    }, realization_checks={
        "independent_oracle": "manifest.explainability.independent_verification_rate",
        "learning_gate": "manifest.learning.publication_gate",
        "redaction_gate": "redaction-report.json",
    })

    verdict = comparability_verdict(_assessment(old), _assessment(new, assessment_id="assessment-crossapp-2"))

    assert not verdict.comparable
    assert verdict.as_dict()["blockers"] == ["methodology_semantic_hash_changed"]


def test_publication_decision_is_derived_from_gates_not_assessment_metrics():
    methodology = _methodology()
    assessment = _assessment(methodology)
    decision = publication_decision_from_gates(
        surface="reliability-lab",
        assessment=assessment,
        gates=[
            PublicationGate("oracle_replay", PublicationGateStatus.PASS, "manifest.explainability", "Replay passes."),
            PublicationGate("cross_platform", PublicationGateStatus.PENDING, "cross-platform.json", "Live OS reports needed."),
        ],
        reason="Static bundle is reviewable; live evidence remains pending.",
    )

    payload = decision.as_dict()
    assert decision.validation_errors() == ()
    assert payload["decision"] == "PENDING_LIVE_EVIDENCE"
    assert payload["publishable_static_bundle"] is True
    assert payload["publishable_full_benchmark_claim"] is False
    assert payload["pending"] == ["cross_platform"]


def test_errata_register_allows_clean_register_and_stable_digest():
    register = ErrataRegister(
        suite_id="crossapp-lane-a",
        suite_version="2026.10",
        surface="reliability-lab",
    )

    assert register.validation_errors() == ()
    assert register.canonical_payload()["entry_count"] == 0
    assert register.digest() == ErrataRegister(
        suite_id="crossapp-lane-a",
        suite_version="2026.10",
        surface="reliability-lab",
    ).digest()


def test_erratum_requires_affected_artifacts_and_correction_lineage():
    corrected = PublicationErratum(
        erratum_id="erratum-1",
        status=ErratumStatus.CORRECTED,
        discovered_at="2026-10-12T00:00:00Z",
        affected_artifacts=("assessment.json",),
        reason="Metric label was wrong.",
        correction_summary="Renamed metric without changing values.",
        replacement_refs=("assessment.json@sha256:new",),
    )
    bad = PublicationErratum(
        erratum_id="erratum-2",
        status=ErratumStatus.SUPERSEDED,
        discovered_at="2026-10-12T00:00:00Z",
        affected_artifacts=(),
        reason="",
        correction_summary="",
        severity="weird",
    )

    assert corrected.validation_errors() == ()
    assert corrected.canonical_payload()["contract_version"] == "assessment-publication/v1"
    assert bad.validation_errors() == (
        "reason is required",
        "correction_summary is required",
        "affected_artifacts are required",
        "replacement_refs are required for corrected or superseded errata",
        "unknown erratum severity 'weird'",
    )
