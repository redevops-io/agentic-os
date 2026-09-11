"""Acceptance tests for the research-planner backtest (agentic_os.research_planner_backtest).

The verification for the Information-Gain Planner (plan §10/§21): on controlled tasks with a known
ground truth and a fixed budget, planning by expected information gain should reach confident, CORRECT
conclusions far more often — and more cheaply — than naive selection (random / round-robin). Asserted
over a seed sweep with conservative floors.

Observed over seeds 0..39: info_gain decided≈0.70 correct≈0.85 cost≈3.5; random 0.15/0.61/4.4;
round_robin 0.01/0.31/4.6. info_gain beats both naive strategies on decided-rate on 40/40 seeds.
(Cost-blind greedy_eig is comparable to info_gain here — both are information-gain planning, and the
thorough/expensive investigations are unaffordable within budget — so the honest claim is the win over
NAIVE selection, not over the other information-gain arm.)

As with the other kernels this validates the planning LOGIC on a controlled benchmark; it is NOT
real-world research skill.
"""
from __future__ import annotations

import statistics

import pytest

from agentic_os.research_planner_backtest import make_research_benchmark, run_acceptance

SEEDS = tuple(range(10))


def test_benchmark_is_deterministic():
    a = make_research_benchmark(7)
    b = make_research_benchmark(7)
    assert [(t.ground_truth, t.budget, t.fixed_outcomes) for t in a] == \
           [(t.ground_truth, t.budget, t.fixed_outcomes) for t in b]


def test_tasks_are_well_formed():
    for t in make_research_benchmark(7):
        assert t.ground_truth in t.hypotheses
        assert set(t.fixed_outcomes) == {inv.id for inv in t.investigations}
        assert t.budget > 0


def test_info_gain_planning_beats_naive_selection_on_every_seed():
    for seed in SEEDS:
        r = run_acceptance(seed)
        ig, rnd, rr = r["info_gain"], r["random"], r["round_robin"]
        # reaches a confident conclusion far more often than either naive strategy
        assert ig.decided_rate - rnd.decided_rate >= 0.25, f"seed {seed}: vs random {ig.decided_rate:.2f}/{rnd.decided_rate:.2f}"
        assert ig.decided_rate - rr.decided_rate >= 0.35, f"seed {seed}: vs round_robin"
        # and lands on the true hypothesis more often
        assert ig.correct_rate - rnd.correct_rate >= 0.10, f"seed {seed}: correct {ig.correct_rate:.2f}/{rnd.correct_rate:.2f}"
        assert ig.correct_rate > rr.correct_rate


def test_info_gain_reaches_useful_absolute_performance():
    for seed in SEEDS:
        ig = run_acceptance(seed)["info_gain"]
        assert ig.decided_rate >= 0.5, f"seed {seed}: decided {ig.decided_rate:.2f}"
        assert ig.correct_rate >= 0.70, f"seed {seed}: correct {ig.correct_rate:.2f}"


def test_info_gain_is_at_least_as_cost_efficient_as_random():
    for seed in SEEDS:
        r = run_acceptance(seed)
        assert r["info_gain"].mean_cost <= r["random"].mean_cost + 1e-9


def test_advantage_is_material_in_the_mean():
    ig = [run_acceptance(s)["info_gain"].correct_rate for s in SEEDS]
    rnd = [run_acceptance(s)["random"].correct_rate for s in SEEDS]
    assert statistics.mean(ig) - statistics.mean(rnd) >= 0.15


def test_information_gain_principle_holds_for_both_planning_arms():
    # both info-gain arms (cost-aware and cost-blind) should beat naive selection — it's the PRINCIPLE
    # of planning by information gain that wins, not one particular ranking.
    for seed in SEEDS:
        r = run_acceptance(seed)
        assert r["info_gain"].decided_rate > r["random"].decided_rate
        assert r["greedy_eig"].decided_rate > r["random"].decided_rate
