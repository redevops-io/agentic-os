"""Tests for the outcome-learning loop (agentic_os.outcome_learning + its backtest).

The consequential claim: the stack can observe outcomes and change future selection — while preserving
governance, replayability, and explainability. These test the learner directly and assert the backtest
acceptance targets over a seed sweep.
"""
from __future__ import annotations

import statistics

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.outcome_learning import UtilityModel
from agentic_os.priority_engine import (
    Action, DecisionOpportunity, InterventionCandidate, OutcomeEvent, OutcomeLog, select_action)

SEEDS = tuple(range(8))


def _log(*triples):
    log = OutcomeLog()
    for app, kind, reward in triples:
        log.record(OutcomeEvent(f"{app}:{kind}", app, Action.ACT, action_kind=kind, observed_reward=reward))
    return log


# ── the model ─────────────────────────────────────────────────────────────────────────
def test_no_history_returns_the_prior_unchanged():
    m = UtilityModel().fit(OutcomeLog())
    c = InterventionCandidate("crm", "s", "do send", 0.5, 0.8, action_kind="send")
    assert m.utility(c, base=0.42) == pytest.approx(0.42)


def test_utility_blends_toward_observed_mean_with_more_evidence():
    c = InterventionCandidate("crm", "s", "do send", 0.5, 0.8, action_kind="send")
    thin = UtilityModel().fit(_log(("crm", "send", 1.0)))                       # 1 outcome
    thick = UtilityModel().fit(_log(*[("crm", "send", 1.0)] * 50))              # 50 outcomes
    # both move the base (0.0) toward the observed mean (1.0); more evidence moves it further
    assert 0.0 < thin.utility(c, 0.0) < thick.utility(c, 0.0) < 1.0


def test_attribution_confidence_weights_the_evidence():
    c = InterventionCandidate("crm", "s", "do send", 0.5, 0.8, action_kind="send")
    log = OutcomeLog()
    log.record(OutcomeEvent("crm:send", "crm", Action.ACT, action_kind="send", observed_reward=1.0,
                            attribution_confidence=0.2))
    m = UtilityModel().fit(log)
    mean, n = m.observed(("crm", "send"))
    assert mean == pytest.approx(1.0) and n == pytest.approx(0.2)               # count weighted by attribution


def test_explain_states_prior_observed_and_blend():
    c = InterventionCandidate("crm", "s", "do send", 0.5, 0.8, action_kind="send")
    m = UtilityModel().fit(_log(("crm", "send", 0.8), ("crm", "send", 0.6)))
    text = m.explain(c, 0.3).lower()
    assert "observed mean" in text and "send" in text


def test_learned_utility_changes_the_selected_action():
    # 'flashy' has the better prior; 'reliable' has the better track record → learning picks reliable
    flashy = InterventionCandidate("crm", "s", "do flashy", 0.9, 0.8, action_kind="flashy",
                                   risk_tier=RiskTier.BOUNDED_WRITE, candidate_id="crm:flashy")
    reliable = InterventionCandidate("crm", "s", "do reliable", 0.3, 0.8, action_kind="reliable",
                                     risk_tier=RiskTier.BOUNDED_WRITE, candidate_id="crm:reliable")
    opp = DecisionOpportunity("s", "crm", (flashy, reliable), opportunity_id="o")
    assert select_action(opp).action.action_kind == "flashy"                   # static: prior wins
    model = UtilityModel().fit(_log(*([("crm", "flashy", -0.2)] * 30 + [("crm", "reliable", 0.9)] * 30)))
    assert select_action(opp, utility_fn=model.as_utility_fn()).action.action_kind == "reliable"


# ── backtest acceptance ────────────────────────────────────────────────────────────────
def test_learning_beats_static_and_lowers_regret():
    from agentic_os.outcome_learning_backtest import run_acceptance
    for seed in SEEDS:
        r = run_acceptance(seed)
        assert r["learning"].total_reward > r["static"].total_reward * 1.3, seed
        assert r["learning"].total_regret < r["static"].total_regret * 0.6, seed


def test_learning_advantage_is_material_in_the_mean():
    from agentic_os.outcome_learning_backtest import run_acceptance
    lw = [run_acceptance(s)["learning"].total_reward for s in SEEDS]
    sw = [run_acceptance(s)["static"].total_reward for s in SEEDS]
    assert statistics.mean(lw) - statistics.mean(sw) >= 50.0


def test_learning_is_replayable():
    from agentic_os.outcome_learning_backtest import run_episode
    for seed in (7, 13):
        assert run_episode(seed, learn=True) == run_episode(seed, learn=True)   # pure fn of the log


def test_learning_preserves_governance():
    from agentic_os.outcome_learning_backtest import consequential_still_needs_approval
    assert consequential_still_needs_approval()                                 # approval gate survives learning


def test_learning_learns_to_abstain_when_all_actions_are_negative():
    from agentic_os.outcome_learning_backtest import learns_to_abstain_when_all_actions_are_negative
    acted_before, abstained_after = learns_to_abstain_when_all_actions_are_negative()
    assert acted_before and abstained_after
