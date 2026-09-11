"""Acceptance tests for the knowledge-frontier backtest (agentic_os.knowledge_frontier_backtest).

Verification for the Knowledge Frontier (plan §12/§21): on controlled learners with a known concept
graph and a fixed learning dynamic, frontier selection should reach materially more importance-weighted
mastery within a fixed teaching budget than naive orderings — chiefly by never wasting a step on a
concept whose prerequisites aren't met. Asserted over a seed sweep with conservative floors.

Observed over seeds 0..29: frontier mastery≈0.37 (0 wasted steps) vs random 0.14 (5.6 wasted),
linear 0.06 (6.9 wasted), importance_only 0.32; frontier beats every baseline on 30/30 seeds.

As with the other kernels this validates the planning LOGIC on a controlled benchmark; it is NOT
real-world pedagogy.
"""
from __future__ import annotations

import statistics

import pytest

from agentic_os.knowledge_frontier_backtest import make_frontier_benchmark, run_acceptance

SEEDS = tuple(range(12))


def test_benchmark_is_deterministic():
    a = make_frontier_benchmark(7)
    b = make_frontier_benchmark(7)
    assert [(t.order, t.budget, tuple(t.graph.concepts)) for t in a] == \
           [(t.order, t.budget, tuple(t.graph.concepts)) for t in b]


def test_tasks_have_prerequisite_structure():
    tasks = make_frontier_benchmark(7)
    assert any(c.prerequisites for t in tasks for c in t.graph.concepts.values())   # real DAGs, not flat


def test_frontier_beats_naive_orderings_on_every_seed():
    for seed in SEEDS:
        r = run_acceptance(seed)
        f, rnd, lin = r["frontier"], r["random"], r["linear"]
        assert f.mean_mastery - rnd.mean_mastery >= 0.10, f"seed {seed}: vs random {f.mean_mastery:.2f}/{rnd.mean_mastery:.2f}"
        assert f.mean_mastery - lin.mean_mastery >= 0.15, f"seed {seed}: vs linear"


def test_frontier_edges_out_a_strong_importance_only_heuristic():
    # importance-only is a strong baseline (it also front-loads foundations); the frontier still wins
    # consistently by additionally weighting uncertainty/retention and finishing half-learned concepts.
    for seed in SEEDS:
        r = run_acceptance(seed)
        assert r["frontier"].mean_mastery >= r["importance_only"].mean_mastery + 0.01, f"seed {seed}"


def test_frontier_never_wastes_a_step_on_a_blocked_concept():
    # the structural guarantee: the frontier only teaches concepts whose prerequisites are met
    for seed in SEEDS:
        assert run_acceptance(seed)["frontier"].mean_wasted_steps <= 0.01, seed


def test_naive_orderings_genuinely_waste_steps():
    # not a strawman: the benchmark's prerequisite structure really does trip prereq-ignoring strategies
    for seed in SEEDS:
        r = run_acceptance(seed)
        assert r["random"].mean_wasted_steps >= 1.0 and r["linear"].mean_wasted_steps >= 1.0, seed


def test_advantage_is_material_in_the_mean():
    f = [run_acceptance(s)["frontier"].mean_mastery for s in SEEDS]
    rnd = [run_acceptance(s)["random"].mean_mastery for s in SEEDS]
    assert statistics.mean(f) - statistics.mean(rnd) >= 0.15
