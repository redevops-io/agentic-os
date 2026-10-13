from agentic_os.mission.apps_e2e import (
    APP_CORE_IDS,
    AppsE2EScenario,
    DecisionModelLane,
    OracleVerdict,
    ScoreSemantics,
    coverage_matrix,
)


def test_app_core_inventory_matches_the_unified_plan():
    assert set(APP_CORE_IDS) == {
        "twenty", "chatwoot", "erpnext", "lago", "metabase", "postiz", "umami", "listmonk",
        "changedetection", "crowdsec", "openscap",
    }


def test_decision_model_lane_validates_pins_semantics_and_latency():
    good = DecisionModelLane(
        provider_id="local-decision-model",
        model_pin="dm-insurance-triage@sha256:abc",
        score_semantics=ScoreSemantics.CLASS_PROB,
        latency_budget_ms=75,
    )
    assert good.is_valid()

    bad = DecisionModelLane(provider_id="", model_pin="", score_semantics="maybe", latency_budget_ms=0,
                            data_classes=())
    assert bad.validation_errors() == (
        "provider_id is required",
        "model_pin is required",
        "unknown score_semantics 'maybe'",
        "latency_budget_ms must be positive",
        "at least one allowed data class is required",
    )


def test_scenario_digest_is_canonical_over_ordered_fields():
    lane = DecisionModelLane("mock", "model@1", ScoreSemantics.RANK_SCORE, 50)
    a = AppsE2EScenario(
        id="insurance-renewal-dispute-0001",
        mission_family="insurance_distribution",
        app_ids=("chatwoot", "twenty", "lago"),
        source_dataset="insurance-distribution-fixtures",
        source_digest="sha256:fixture",
        expected_verdicts=(OracleVerdict.CORRECT_HOLD, OracleVerdict.VERIFIED_SUCCESS),
        required_postconditions=("dispute_open", "review_task_exists"),
        forbidden_effects=("unapproved_collection", "duplicate_credit"),
        as_of="2026-01-01T00:00:00Z",
        known_at="2026-01-01T00:00:00Z",
        decision_lane=lane,
    )
    b = AppsE2EScenario(
        id="insurance-renewal-dispute-0001",
        mission_family="insurance_distribution",
        app_ids=("lago", "twenty", "chatwoot"),
        source_dataset="insurance-distribution-fixtures",
        source_digest="sha256:fixture",
        expected_verdicts=(OracleVerdict.VERIFIED_SUCCESS, OracleVerdict.CORRECT_HOLD),
        required_postconditions=("review_task_exists", "dispute_open"),
        forbidden_effects=("duplicate_credit", "unapproved_collection"),
        as_of="2026-01-01T00:00:00Z",
        known_at="2026-01-01T00:00:00Z",
        decision_lane=lane,
    )
    assert a.validation_errors() == ()
    assert a.digest() == b.digest()


def test_coverage_matrix_marks_missing_and_unsupported_apps():
    scenario = AppsE2EScenario(
        id="security-held-for-approval",
        mission_family="security_compliance",
        app_ids=("crowdsec", "openscap"),
        source_dataset="security-fixtures",
        source_digest="sha256:security",
        expected_verdicts=(OracleVerdict.CORRECT_HOLD,),
    )
    matrix = coverage_matrix([scenario], unsupported={"postiz": "publish connector not present in this lane"})
    by_app = {row.app_id: row for row in matrix.rows}
    assert by_app["crowdsec"].scenarios == ("security-held-for-approval",)
    assert by_app["postiz"].unsupported is True
    assert "postiz" not in matrix.missing_required
    assert "twenty" in matrix.missing_required
    assert matrix.complete is False


def test_scenario_validation_refuses_unknown_app_or_verdict():
    scenario = AppsE2EScenario(
        id="bad",
        mission_family="x",
        app_ids=("not-an-app",),
        source_dataset="d",
        source_digest="sha256:x",
        expected_verdicts=("GREEN",),
    )
    assert scenario.validation_errors() == (
        "unknown app_ids: not-an-app",
        "unknown oracle verdict 'GREEN'",
    )
