import pytest

from agentic_os.mission.apps_e2e import (
    APP_CORE_IDS,
    AppsE2EScenario,
    DecisionModelLane,
    OracleVerdict,
    ScoreSemantics,
)
from agentic_os.mission.apps_e2e_runner import (
    AppExecutionOutcome,
    InMemoryEpisodeStore,
    JsonlEpisodeStore,
    evaluation_program_for_suite,
    run_scenario,
    scenario_run_from_payload,
    summarize_runs,
)
from agentic_os.mission.decision_provider import DecisionRequest, StaticDecisionProvider


def _scenario(**kwargs) -> AppsE2EScenario:
    base = dict(
        id="insurance-cross-app-1",
        mission_family="insurance_distribution",
        app_ids=APP_CORE_IDS,
        source_dataset="synthetic-insurance",
        source_digest="sha256:dataset",
        expected_verdicts=(OracleVerdict.VERIFIED_SUCCESS, OracleVerdict.CORRECT_HOLD),
        required_postconditions=("twenty:lead-updated", "chatwoot:message-recorded"),
        forbidden_effects=("lago:duplicate-invoice",),
        as_of="2026-01-01T00:00:00Z",
        known_at="2026-01-01T00:00:00Z",
    )
    base.update(kwargs)
    return AppsE2EScenario(**base)


def _request(**kwargs) -> DecisionRequest:
    base = dict(
        decision_id="dec-1",
        tenant_id="tenant-a",
        purpose="insurance_distribution",
        question_kind="choice",
        evidence_view_ref="cv-insurance",
        permitted_options=("HOLD", "FOLLOW_UP", "COLLECT"),
        constraints_ref="policy@sha256:abc",
        as_of="2026-01-01T00:00:00Z",
        known_at="2026-01-01T00:00:00Z",
        latency_budget_ms=50,
        data_classification="synthetic",
    )
    base.update(kwargs)
    return DecisionRequest(**base)


def _outcome(verdict=OracleVerdict.VERIFIED_SUCCESS, **kwargs) -> AppExecutionOutcome:
    base = dict(
        oracle_verdict=verdict,
        execution_ref="mission:1",
        execution_time="2026-01-01T00:00:03Z",
        readback_ref="readback:1",
        observation_time="2026-01-02T00:00:00Z",
        evaluation_cutoff="2026-01-03T00:00:00Z",
        outcome_ref="outcome:1",
        reward={"renewed": 1.0},
    )
    base.update(kwargs)
    return AppExecutionOutcome(**base)


def test_evaluation_program_pins_suite_digest_and_protocol():
    scenario = _scenario()
    program = evaluation_program_for_suite("apps-e2e", "2026.10", (scenario,))
    assert program.dataset.content_hash == program.dataset.compatibility_hash
    assert program.protocol.verification_policy == "independent-readback-required"
    assert program.steps == ("insurance-cross-app-1",)


def test_run_scenario_records_learning_eligible_decision_episode():
    scenario = _scenario()
    request = _request()
    provider = StaticDecisionProvider("static", "static@1")

    run = run_scenario(
        scenario,
        request,
        provider,
        lambda s, r: _outcome(),
        tenant_id="tenant-a",
        behavior_policy_id="benchmark-router",
        behavior_policy_pin="router@sha256:abc",
        propensity=1.0,
        decision_time="2026-01-01T00:00:01Z",
    )

    assert run.scenario_digest == scenario.digest()
    assert run.request_digest == request.digest()
    assert run.result_digest
    assert run.episode.learning_eligible()


def test_scenario_run_payload_round_trips_with_digest_checks():
    scenario = _scenario()
    request = _request()
    provider = StaticDecisionProvider("static", "static@1")
    run = run_scenario(
        scenario,
        request,
        provider,
        lambda s, r: _outcome(),
        tenant_id="tenant-a",
        behavior_policy_id="benchmark-router",
        behavior_policy_pin="router@sha256:abc",
        propensity=1.0,
        decision_time="2026-01-01T00:00:01Z",
    )

    restored = scenario_run_from_payload(run.canonical_payload())
    assert restored.digest() == run.digest()
    assert restored.episode.digest() == run.episode.digest()

    tampered = dict(run.canonical_payload())
    tampered["episode_digest"] = "sha256:wrong"
    with pytest.raises(ValueError, match="episode_digest"):
        scenario_run_from_payload(tampered)


def test_episode_stores_are_append_only_and_replayable(tmp_path):
    scenario = _scenario()
    request = _request()
    provider = StaticDecisionProvider("static", "static@1")
    run = run_scenario(
        scenario,
        request,
        provider,
        lambda s, r: _outcome(),
        tenant_id="tenant-a",
        behavior_policy_id="benchmark-router",
        behavior_policy_pin="router@sha256:abc",
        propensity=1.0,
        decision_time="2026-01-01T00:00:01Z",
    )

    mem = InMemoryEpisodeStore("bench-memory")
    receipt = mem.append(run)
    assert receipt.sequence == 1
    assert receipt.run_digest == run.digest()
    assert mem.bundle().run_count == 1
    assert mem.bundle().episode_digests == (run.episode.digest(),)

    path = str(tmp_path / "episodes" / "runs.jsonl")
    file_store = JsonlEpisodeStore(path, store_id="bench-jsonl")
    file_store.append(run)
    reopened = JsonlEpisodeStore(path, store_id="bench-jsonl")
    assert reopened.bundle().digest() == file_store.bundle().digest()
    assert reopened.runs()[0].digest() == run.digest()


def test_run_scenario_enforces_declared_decision_lane():
    scenario = _scenario(decision_lane=DecisionModelLane(
        provider_id="dm",
        model_pin="small@1",
        score_semantics=ScoreSemantics.RANK_SCORE,
        latency_budget_ms=25,
    ))
    provider = StaticDecisionProvider("static", "static@1")
    with pytest.raises(ValueError, match="provider_id does not match"):
        run_scenario(
            scenario,
            _request(),
            provider,
            lambda s, r: _outcome(),
            tenant_id="tenant-a",
            behavior_policy_id="benchmark-router",
            behavior_policy_pin="router@sha256:abc",
            propensity=1.0,
            decision_time="2026-01-01T00:00:01Z",
        )


def test_summarize_runs_keeps_unresolved_outcomes_out_of_learning_rate():
    scenario = _scenario()
    provider = StaticDecisionProvider("static", "static@1")
    observed = run_scenario(
        scenario,
        _request(decision_id="dec-1"),
        provider,
        lambda s, r: _outcome(OracleVerdict.VERIFIED_SUCCESS),
        tenant_id="tenant-a",
        behavior_policy_id="benchmark-router",
        behavior_policy_pin="router@sha256:abc",
        propensity=0.5,
        decision_time="2026-01-01T00:00:01Z",
    )
    unresolved = run_scenario(
        scenario,
        _request(decision_id="dec-2"),
        provider,
        lambda s, r: _outcome(
            OracleVerdict.UNRESOLVED,
            readback_ref="",
            observation_time="",
            outcome_ref="",
            reward={},
        ),
        tenant_id="tenant-a",
        behavior_policy_id="benchmark-router",
        behavior_policy_pin="router@sha256:abc",
        propensity=0.5,
        decision_time="2026-01-01T00:00:01Z",
    )

    report = summarize_runs("apps-e2e", "2026.10", (scenario,), (observed, unresolved))
    assert report.complete
    assert not report.publishable
    assert report.metrics["verified_success_rate"] == 0.5
    assert report.metrics["unresolved_rate"] == 0.5
    assert report.metrics["learning_eligible_rate"] == 0.5
    assert report.digest()


def test_summarize_runs_exposes_missing_required_app_coverage():
    scenario = _scenario(app_ids=("twenty",))
    report = summarize_runs("apps-e2e", "2026.10", (scenario,), ())
    assert not report.complete
    assert "chatwoot" in report.coverage.missing_required
    assert report.metrics["coverage_complete"] == 0.0
