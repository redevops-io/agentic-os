"""Acceptance tests for the execution-risk backtest (agentic_os.execution_risk_backtest).

The verification the Risk Radar hinges on (plan §5/§21): on historical replay with a fixed cutoff, a
high-confidence threshold should materially increase slip precision over a naive burndown /
blocked-count baseline, with useful lead time. Asserted over a SEED SWEEP with conservative floors —
a property of the method, not a lucky seed. As with the trend kernel, this validates scoring LOGIC on
a controlled benchmark; it is NOT real-world skill (that needs real project histories).

Observed over seeds 0..39 (context; assertions stay well inside):
    ensemble P@K   min 0.93  median 1.00        burndown P@K min 0.30 median 0.49
    ensemble beats BOTH baselines on 40/40 seeds    strict recall min 0.48 max 0.82
"""
from __future__ import annotations

import statistics

import pytest

from agentic_os.execution_risk_backtest import (
    make_project_benchmark, run_acceptance, run_ranking_comparison)

SEEDS = tuple(range(12))


def test_benchmark_is_deterministic():
    a = make_project_benchmark(7)
    b = make_project_benchmark(7)
    assert [(lp.signals.milestone, lp.slipped) for lp in a] == \
           [(lp.signals.milestone, lp.slipped) for lp in b]


def test_benchmark_has_a_realistic_mixed_slip_rate():
    for seed in SEEDS:
        data = make_project_benchmark(seed)
        rate = sum(1 for lp in data if lp.slipped) / len(data)
        assert 0.30 <= rate <= 0.55, f"seed {seed}: slip rate {rate:.2f} outside plausible band"


def test_true_slips_carry_positive_lead_time():
    leads = [lp.lead_days for lp in make_project_benchmark(7) if lp.slipped]
    assert leads and all(ld > 0 for ld in leads)


# ── fair fight #1: matched-volume ranking (calibration-free) ─────────────────────────
def test_ensemble_ranking_beats_baselines_on_every_seed():
    for seed in SEEDS:
        r = run_ranking_comparison(seed)
        ens, bd, bl = r["ensemble"], r["burndown"], r["blocked"]
        assert ens.precision_at_k > bd.precision_at_k, \
            f"seed {seed}: ensemble {ens.precision_at_k:.2f} !> burndown {bd.precision_at_k:.2f}"
        assert ens.precision_at_k > bl.precision_at_k, \
            f"seed {seed}: ensemble {ens.precision_at_k:.2f} !> blocked {bl.precision_at_k:.2f}"
        assert ens.precision_at_k >= 0.85, f"seed {seed}: ensemble P@K {ens.precision_at_k:.2f} below floor"


def test_ensemble_advantage_is_material_in_the_mean():
    ens = [run_ranking_comparison(s)["ensemble"].precision_at_k for s in SEEDS]
    bd = [run_ranking_comparison(s)["burndown"].precision_at_k for s in SEEDS]
    bl = [run_ranking_comparison(s)["blocked"].precision_at_k for s in SEEDS]
    assert statistics.mean(ens) - statistics.mean(bd) >= 0.25   # a burndown misses dependency-driven slips
    assert statistics.mean(ens) - statistics.mean(bl) >= 0.20


def test_baselines_are_genuine_competitors_not_strawmen():
    for seed in SEEDS:
        r = run_ranking_comparison(seed)
        assert r["burndown"].precision_at_k >= 0.25, \
            f"seed {seed}: burndown unrealistically weak ({r['burndown'].precision_at_k:.2f}) — benchmark too separable"
        assert r["blocked"].precision_at_k >= 0.30, \
            f"seed {seed}: blocked baseline unrealistically weak ({r['blocked'].precision_at_k:.2f})"


def test_ensemble_ranking_preserves_useful_lead_time():
    for seed in SEEDS:
        assert run_ranking_comparison(seed)["ensemble"].mean_lead_days >= 20.0


# ── fair fight #2: the calibrated strict-threshold operating point ───────────────────
def test_strict_threshold_gives_high_precision_with_lead_time():
    for seed in SEEDS:
        m = run_acceptance(seed, threshold=0.90)["ensemble"]
        assert m.n_flagged > 0, f"seed {seed}: ensemble flagged nothing at 0.90"
        assert m.precision >= 0.85, f"seed {seed}: strict precision {m.precision:.2f}"
        assert m.mean_lead_days >= 20.0, f"seed {seed}: lead {m.mean_lead_days:.0f}d"


def test_strict_threshold_precision_is_high_in_the_mean():
    ps = [run_acceptance(s, threshold=0.90)["ensemble"].precision for s in SEEDS]
    assert statistics.mean(ps) >= 0.90


def test_strict_threshold_abstains_on_the_hard_cases():
    for seed in SEEDS:
        m = run_acceptance(seed, threshold=0.90)["ensemble"]
        assert m.recall < 0.95, f"seed {seed}: strict recall {m.recall:.2f} suspiciously complete"


def test_calibration_keeps_the_burndown_baseline_out_of_the_strict_band():
    # a burndown score, calibrated against outcomes, rarely reaches the strict band — pace alone does
    # not reliably predict a slip (dependency-driven slips look on-pace), so it can't earn recall there.
    for seed in SEEDS:
        m = run_acceptance(seed, threshold=0.90)["burndown"]
        assert m.recall < 0.5, f"seed {seed}: burndown unexpectedly complete at strict threshold ({m.recall:.2f})"


def test_ensemble_is_well_calibrated():
    for seed in SEEDS:
        assert run_acceptance(seed, threshold=0.90)["ensemble"].calibration_error <= 0.20
