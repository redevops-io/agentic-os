import math

from agentic_os.mission.apps_e2e import OracleVerdict
from agentic_os.mission.decision_episode import DecisionEpisode


def _episode(**kwargs) -> DecisionEpisode:
    base = dict(
        episode_id="ep-1",
        scenario_id="insurance-cross-app-1",
        decision_id="dec-1",
        tenant_id="tenant-a",
        request_digest="sha256:req",
        result_digest="sha256:res",
        eligible_options=("HOLD", "FOLLOW_UP", "COLLECT"),
        selected_option="FOLLOW_UP",
        abstained=False,
        behavior_policy_id="benchmark-router",
        behavior_policy_pin="router@sha256:abc",
        propensity=0.25,
        as_of="2026-01-01T00:00:00Z",
        known_at="2026-01-01T00:00:00Z",
        decision_time="2026-01-01T00:00:01Z",
        execution_ref="mission:1",
        execution_time="2026-01-01T00:00:03Z",
        readback_ref="readback:1",
        observation_time="2026-01-02T00:00:00Z",
        evaluation_cutoff="2026-01-03T00:00:00Z",
        oracle_verdict=OracleVerdict.VERIFIED_SUCCESS,
        outcome_ref="outcome:1",
        reward={"renewed": 1.0, "cost": -0.1},
    )
    base.update(kwargs)
    return DecisionEpisode(**base)


def test_observed_episode_is_learning_eligible_and_digest_stable():
    episode = _episode(metadata={"lane": "decision-model", "arm": "small-model"})
    assert episode.validation_errors() == ()
    assert episode.has_action_support()
    assert episode.has_observed_outcome()
    assert episode.learning_eligible()
    assert episode.digest() == _episode(metadata={"arm": "small-model", "lane": "decision-model"}).digest()


def test_episode_allows_unresolved_outcome_but_not_learning():
    episode = _episode(
        readback_ref="",
        observation_time="",
        oracle_verdict=OracleVerdict.UNRESOLVED,
        outcome_ref="",
        reward={},
    )
    assert episode.validation_errors() == ()
    assert not episode.has_observed_outcome()
    assert not episode.learning_eligible()


def test_observed_verdict_requires_outcome_and_observation_time():
    episode = _episode(outcome_ref="", observation_time="")
    assert episode.validation_errors() == (
        "outcome_ref is required for observed oracle verdicts",
        "observation_time is required for observed oracle verdicts",
    )


def test_rejects_action_and_policy_support_holes():
    episode = _episode(
        eligible_options=("HOLD", "HOLD"),
        selected_option="DELETE",
        propensity=math.inf,
        reward={"renewed": math.nan},
    )
    assert episode.validation_errors() == (
        "eligible_options contains duplicates",
        "selected_option must be one of eligible_options",
        "propensity must be finite",
        "reward value for 'renewed' must be finite",
    )


def test_propensity_missing_is_recordable_but_not_learning_eligible():
    episode = _episode(propensity=None)
    assert episode.validation_errors() == ()
    assert not episode.has_action_support()
    assert not episode.learning_eligible()


def test_abstention_and_censoring_are_fail_closed_for_learning():
    abstained = _episode(selected_option=None, abstained=True)
    assert abstained.validation_errors() == ()
    assert not abstained.learning_eligible()

    censored = _episode(censored=True, censor_reason="outcome window still open")
    assert censored.validation_errors() == ()
    assert not censored.has_observed_outcome()
    assert not censored.learning_eligible()


def test_temporal_order_is_validated_lexically_for_normalized_timestamps():
    episode = _episode(
        as_of="2026-01-02T00:00:00Z",
        known_at="2026-01-01T00:00:00Z",
        decision_time="2026-01-01T00:00:01Z",
        execution_time="2025-12-31T00:00:00Z",
        observation_time="2025-12-30T00:00:00Z",
        evaluation_cutoff="2025-12-29T00:00:00Z",
    )
    assert episode.validation_errors() == (
        "as_of must not be after known_at",
        "decision_time must not be after execution_time",
        "execution_time must not be after observation_time",
        "decision_time must not be after evaluation_cutoff",
    )
