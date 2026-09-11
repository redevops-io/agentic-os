"""Unit tests for the Research Information-Gain Planner (agentic_os.research_planner) + its Priority
Engine adapter. Test the planning LOGIC — entropy/information-gain maths, Bayesian update, the
choose-best-per-cost step, and the stop reasons. Whether the policy beats naive strategies is in
test_research_planner_backtest.py.
"""
from __future__ import annotations

import math

import pytest

from agentic_os.research_planner import (
    Belief, Investigation, ResearchAction, ResearchPolicy, StopReason, expected_information_gain,
    plan, update)


def _diag(id, favored, hyps, sharp=0.9, low=0.1, cost=1.0):
    return Investigation(id=id, cost=cost,
                         pos_likelihoods={h: (sharp if h == favored else low) for h in hyps})


def _useless(id, hyps, cost=1.0):
    return Investigation(id=id, cost=cost, pos_likelihoods={h: 0.5 for h in hyps})


HYPS = ("A", "B", "C", "D")


# ── belief / entropy ──────────────────────────────────────────────────────────────────
def test_uniform_belief_has_max_entropy_and_normalises():
    b = Belief.uniform(HYPS)
    assert b.entropy == pytest.approx(math.log2(4))
    assert sum(b.probs.values()) == pytest.approx(1.0)
    assert Belief({"A": 2, "B": 2}).probs["A"] == pytest.approx(0.5)   # normalised on construction


def test_certain_belief_has_zero_entropy():
    assert Belief({"A": 0.999999, "B": 1e-6}).entropy < 0.001


# ── information gain ──────────────────────────────────────────────────────────────────
def test_information_gain_positive_for_diagnostic_zero_for_useless():
    b = Belief.uniform(HYPS)
    assert expected_information_gain(b, _diag("d", "A", HYPS)) > 0.3
    assert expected_information_gain(b, _useless("u", HYPS)) < 1e-6


def test_information_gain_never_negative():
    b = Belief({"A": 0.7, "B": 0.2, "C": 0.1})
    for inv in (_diag("d", "B", ("A", "B", "C")), _useless("u", ("A", "B", "C"))):
        assert expected_information_gain(b, inv) >= 0.0


# ── Bayesian update ───────────────────────────────────────────────────────────────────
def test_update_shifts_toward_the_favored_hypothesis_on_positive():
    b = Belief.uniform(HYPS)
    post = update(b, _diag("d", "A", HYPS), positive=True)
    assert post.probs["A"] > b.probs["A"] and post.leading[0] == "A"


def test_update_shifts_away_on_negative():
    b = Belief.uniform(HYPS)
    post = update(b, _diag("d", "A", HYPS), positive=False)
    assert post.probs["A"] < b.probs["A"]


def test_two_consistent_diagnostics_can_reach_decision_confidence():
    b = Belief.uniform(HYPS)
    d = _diag("d", "A", HYPS, sharp=0.92, low=0.05)
    b = update(update(b, d, True), _diag("d2", "A", HYPS, sharp=0.92, low=0.05), True)
    assert b.leading[0] == "A" and b.leading[1] > 0.9


# ── the planning step ─────────────────────────────────────────────────────────────────
def test_plan_investigates_the_best_gain_per_cost():
    b = Belief.uniform(HYPS)
    cheap = _diag("cheap", "A", HYPS, cost=1.0)
    pricey = _diag("pricey", "B", HYPS, cost=5.0)      # same info, 5x the cost
    step = plan(b, [cheap, pricey], budget_remaining=10.0)
    assert step.action == ResearchAction.INVESTIGATE and step.investigation.id == "cheap"
    assert step.expected_gain > 0


def test_plan_stops_when_decision_threshold_reached():
    b = Belief({"A": 0.95, "B": 0.02, "C": 0.02, "D": 0.01})
    step = plan(b, [_diag("d", "A", HYPS)], budget_remaining=10.0)
    assert step.action == ResearchAction.STOP and step.stop_reason == StopReason.DECISION_REACHED
    assert step.decision == "A"


def test_plan_stops_budget_exhausted_when_nothing_affordable():
    b = Belief.uniform(HYPS)
    step = plan(b, [_diag("d", "A", HYPS, cost=9.0)], budget_remaining=2.0)
    assert step.action == ResearchAction.STOP and step.stop_reason == StopReason.BUDGET_EXHAUSTED


def test_plan_stops_irreducibly_ambiguous_when_only_useless_remain():
    b = Belief.uniform(HYPS)
    step = plan(b, [_useless("u1", HYPS), _useless("u2", HYPS)], budget_remaining=10.0)
    assert step.action == ResearchAction.STOP and step.stop_reason == StopReason.IRREDUCIBLY_AMBIGUOUS


# ── the Priority Engine adapter ───────────────────────────────────────────────────────
def test_research_adapter_builds_a_low_risk_auto_candidate_for_an_investigation():
    from agentic_os.agent_gateway.contracts import RiskTier
    from agentic_os.priority_engine import Action, decide, from_research_plan
    b = Belief.uniform(HYPS)
    step = plan(b, [_diag("cheap", "A", HYPS, cost=1.0)], budget_remaining=10.0)
    c = from_research_plan(step, question="which retriever is best?")
    assert c is not None and c.risk_tier == RiskTier.READ
    assert c.information_value > 0 and "cheap" in c.proposed_action
    # gathering evidence is low-risk ⇒ it runs automatically (the §17 'acquire evidence' move)
    assert decide(c).action == Action.ACT


def test_research_adapter_returns_none_for_a_stop_step():
    from agentic_os.priority_engine import from_research_plan
    b = Belief({"A": 0.95, "B": 0.05})
    step = plan(b, [_diag("d", "A", ("A", "B"))], budget_remaining=10.0)   # STOP: decided
    assert from_research_plan(step) is None
