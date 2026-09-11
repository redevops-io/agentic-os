"""Unit tests for the deterministic trend-intelligence kernel (agentic_os.trend_intelligence).

These test the scoring *logic* — that each detector measures what the plan says it measures, that the
calibrator is a proper monotone isotonic fit, and that ``assess`` abstains when the evidence is thin,
manipulated, or below the mode threshold. They make NO real-world accuracy claim; the discriminative
power of the ensemble is tested on a controlled replay benchmark in test_trend_backtest.py.
"""
from __future__ import annotations

import math

import pytest

from agentic_os.trend_intelligence import (
    IsotonicCalibrator, LifecycleStage, Mode, TrendCandidate, acceleration,
    assess, audience_question_growth, capitalization_gap, competition,
    creator_outlier_strength, cross_source_confirmation, geographic_spread,
    lifecycle, manipulation_risk, raw_future_confidence, saturation,
    signal_vector, source_concentration, source_independence, velocity)


# ── series helpers ────────────────────────────────────────────────────────────────────
def _rising(base=1.0, growth=0.3, n=8):
    out, v = [base], base
    for _ in range(n - 1):
        v *= (1 + growth)
        out.append(v)
    return out


def _flat(level=5.0, n=8):
    return [level] * n


# ── velocity / acceleration ─────────────────────────────────────────────────────────
def test_velocity_positive_for_rising_zero_for_flat():
    rising = TrendCandidate(entity="a", series={"reddit": _rising()})
    flat = TrendCandidate(entity="b", series={"reddit": _flat()})
    assert velocity(rising) > 0.2
    assert velocity(flat) == pytest.approx(0.0, abs=1e-9)


def test_acceleration_higher_when_growth_speeds_up():
    steady = TrendCandidate(entity="s", series={"reddit": _rising(growth=0.3)})
    # a series whose step size keeps increasing (convex) accelerates harder than constant-ratio growth
    convex = TrendCandidate(entity="c", series={"reddit": [1, 1.1, 1.3, 1.7, 2.5, 4.0, 7.0, 13.0]})
    assert acceleration(convex) > acceleration(steady)


# ── cross-source confirmation is a COUNT, not a fraction ─────────────────────────────
def test_cross_source_confirmation_counts_rising_sources():
    one = TrendCandidate(entity="1", series={"reddit": _rising()})
    three = TrendCandidate(entity="3", series={s: _rising() for s in ("reddit", "youtube", "search")})
    # a lone rising source is weak (1/3); three agreeing is the full signal
    assert cross_source_confirmation(one) == pytest.approx(1 / 3, abs=1e-6)
    assert cross_source_confirmation(three) == pytest.approx(1.0, abs=1e-6)


def test_cross_source_confirmation_ignores_flat_sources():
    mixed = TrendCandidate(entity="m", series={"reddit": _rising(), "youtube": _flat(), "search": _flat()})
    assert cross_source_confirmation(mixed) == pytest.approx(1 / 3, abs=1e-6)


# ── source independence / concentration ──────────────────────────────────────────────
def test_source_independence_low_when_one_source_dominates():
    balanced = TrendCandidate(entity="b", series={"reddit": _flat(10), "youtube": _flat(10)})
    lopsided = TrendCandidate(entity="l", series={"reddit": _flat(100), "youtube": _flat(1)})
    assert source_independence(balanced) > source_independence(lopsided)
    assert source_concentration(lopsided) == pytest.approx(1.0 - source_independence(lopsided))


def test_source_independence_zero_for_single_source():
    assert source_independence(TrendCandidate(entity="s", series={"reddit": _rising()})) == 0.0


# ── creator outliers require MULTIPLE examples ───────────────────────────────────────
def test_creator_outlier_needs_at_least_two_strong():
    one = TrendCandidate(entity="1", series={"reddit": _rising()}, creator_outliers=(10.0,))
    two = TrendCandidate(entity="2", series={"reddit": _rising()}, creator_outliers=(10.0, 8.0))
    assert creator_outlier_strength(one) == 0.0          # a single outlier is anecdote, not signal
    assert creator_outlier_strength(two) > 0.5


# ── other detectors ──────────────────────────────────────────────────────────────────
def test_geographic_spread_and_question_growth_monotone():
    assert geographic_spread(TrendCandidate(entity="g1", series={}, geo_count=1)) == 0.0
    assert geographic_spread(TrendCandidate(entity="g6", series={}, geo_count=6)) == pytest.approx(1.0)
    assert audience_question_growth(TrendCandidate(entity="q", series={}, question_growth=1.5)) == pytest.approx(1.0)


def test_manipulation_risk_penalizes_single_source_spike_and_bots():
    clean = TrendCandidate(entity="c", series={"reddit": _rising()})
    spike = TrendCandidate(entity="s", series={"reddit": _rising()}, single_source_spike=True, bot_ratio=0.5)
    assert manipulation_risk(clean) == 0.0
    assert manipulation_risk(spike) > 0.6


def test_competition_and_saturation_average_their_inputs():
    c = TrendCandidate(entity="c", series={}, competitor_authority=0.9, keyword_competition=0.9,
                       brand_penetration=0.9, content_supply=0.8, ad_density=0.8)
    assert competition(c) == pytest.approx(0.9)
    assert saturation(c) == pytest.approx(0.8)


# ── the ensemble score ────────────────────────────────────────────────────────────────
def test_signal_vector_has_all_weighted_signals():
    from agentic_os.trend_intelligence import DEFAULT_WEIGHTS
    sig = signal_vector(TrendCandidate(entity="x", series={"reddit": _rising()}))
    assert set(sig) == set(DEFAULT_WEIGHTS)


def test_raw_confidence_rewards_broad_emergence_and_punishes_noise():
    strong = TrendCandidate(
        entity="strong", series={s: _rising() for s in ("reddit", "youtube", "search")},
        creator_outliers=(8.0, 6.0, 5.0), question_growth=1.0, geo_count=5)
    noisy = TrendCandidate(
        entity="noisy", series={"reddit": _rising()}, single_source_spike=True, bot_ratio=0.6,
        competitor_authority=0.8, content_supply=0.8, ad_density=0.8)
    assert raw_future_confidence(strong) > 0.6
    assert raw_future_confidence(noisy) < 0.25
    assert raw_future_confidence(strong) > raw_future_confidence(noisy)


def test_raw_confidence_bounded_unit_interval():
    for c in (TrendCandidate(entity="z", series={}),
              TrendCandidate(entity="s", series={s: _rising() for s in ("a", "b", "c")},
                             creator_outliers=(20.0, 20.0), question_growth=2.0, geo_count=9)):
        assert 0.0 <= raw_future_confidence(c) <= 1.0


# ── isotonic calibrator (PAV) ────────────────────────────────────────────────────────
def test_calibrator_is_identity_until_fit():
    cal = IsotonicCalibrator()
    assert cal.transform(0.42) == pytest.approx(0.42)


def test_calibrator_is_monotone_nondecreasing():
    cal = IsotonicCalibrator().fit(
        scores=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
        outcomes=[0, 0, 1, 0, 1, 1, 1, 1, 1])
    xs = [i / 100 for i in range(0, 101)]
    ys = [cal.transform(x) for x in xs]
    assert all(b >= a - 1e-9 for a, b in zip(ys, ys[1:]))   # never decreases
    assert ys[0] <= ys[-1]


def test_calibrator_recovers_empirical_rate_direction():
    # low scores mostly did not break out, high scores mostly did
    cal = IsotonicCalibrator().fit(
        scores=[0.05, 0.1, 0.15, 0.2] + [0.8, 0.85, 0.9, 0.95],
        outcomes=[0, 0, 0, 0] + [1, 1, 1, 1])
    assert cal.transform(0.1) < 0.25
    assert cal.transform(0.9) > 0.75


# ── capitalization gap ────────────────────────────────────────────────────────────────
def test_capitalization_gap_shrinks_as_market_crowds():
    demand = {"series": {s: _rising() for s in ("reddit", "youtube")}, "question_growth": 1.0}
    open_market = TrendCandidate(entity="open", **demand)
    crowded = TrendCandidate(entity="crowded", competitor_authority=0.9, keyword_competition=0.9,
                             brand_penetration=0.9, content_supply=0.9, ad_density=0.9, **demand)
    assert capitalization_gap(open_market) > capitalization_gap(crowded)


# ── lifecycle ─────────────────────────────────────────────────────────────────────────
def test_lifecycle_saturated_and_latent_extremes():
    saturated = TrendCandidate(entity="sat", series={"reddit": _flat(30)},
                               content_supply=0.9, ad_density=0.9)
    latent = TrendCandidate(entity="lat", series={"reddit": _flat(3)})
    assert lifecycle(saturated) == LifecycleStage.SATURATED
    assert lifecycle(latent) == LifecycleStage.LATENT


# ── assess: abstention gates ─────────────────────────────────────────────────────────
def test_assess_abstains_on_thin_evidence():
    c = TrendCandidate(entity="thin", series={"reddit": _rising()})   # one source < MIN_SOURCES
    r = assess(c, mode=Mode.EXPLORATORY)
    assert r.abstained
    assert any("thin" in reason for reason in r.reasons)


def test_assess_abstains_on_manipulation():
    c = TrendCandidate(entity="manip", series={s: _rising() for s in ("reddit", "youtube")},
                       single_source_spike=True, bot_ratio=0.7)
    r = assess(c, mode=Mode.EXPLORATORY)
    assert r.abstained
    assert any("manipulation" in reason for reason in r.reasons)


def test_assess_abstains_below_mode_threshold():
    weak = TrendCandidate(entity="weak", series={s: _flat(5) for s in ("reddit", "youtube")})
    r = assess(weak, mode=Mode.STRICT)
    assert r.abstained
    assert any("threshold" in reason for reason in r.reasons)


def test_assess_surfaces_strong_clean_candidate_in_exploratory():
    strong = TrendCandidate(
        entity="strong", series={s: _rising() for s in ("reddit", "youtube", "search")},
        creator_outliers=(8.0, 6.0), question_growth=1.0, geo_count=4)
    r = assess(strong, mode=Mode.EXPLORATORY)
    assert not r.abstained
    assert r.reasons == ()
    assert r.confidence >= 0.55


def test_assess_calibrated_confidence_differs_from_raw_when_calibrator_supplied():
    cal = IsotonicCalibrator().fit([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1])
    c = TrendCandidate(entity="c", series={s: _rising() for s in ("reddit", "youtube")})
    r = assess(c, calibrator=cal, mode=Mode.EXPLORATORY)
    assert r.raw_score == pytest.approx(raw_future_confidence(c))
    assert r.confidence == pytest.approx(cal.transform(r.raw_score))


def test_stricter_mode_never_surfaces_more_than_looser_mode():
    c = TrendCandidate(entity="c", series={s: _rising() for s in ("reddit", "youtube")},
                       creator_outliers=(6.0, 5.0), question_growth=0.8, geo_count=3)
    strict = assess(c, mode=Mode.STRICT)
    balanced = assess(c, mode=Mode.BALANCED)
    exploratory = assess(c, mode=Mode.EXPLORATORY)
    # abstaining is monotone in strictness: if strict surfaces it, so must the looser modes
    order = [strict.abstained, balanced.abstained, exploratory.abstained]
    assert order == sorted(order, reverse=True)
