"""Unit tests for the Projects Execution Risk Radar kernel (agentic_os.execution_risk) + its Priority
Engine adapter. These test the scoring LOGIC — detectors measure what the plan says, the slip window
and primary cause are sane, and ``assess`` abstains on thin evidence / below threshold. Discriminative
power vs baselines is tested on a controlled replay benchmark in test_execution_risk_backtest.py.
"""
from __future__ import annotations

import pytest

from agentic_os.execution_risk import (
    IsotonicCalibrator, Mode, ProjectSignals, RiskLevel, assess, blocked_dependencies,
    decision_debt, dependency_lag, primary_cause, progress_deceleration, raw_risk,
    schedule_pressure, signal_vector, slip_window_days, stale_approval, under_testing)


def _steady(rate, n=6, start=0.1):
    return tuple(round(start + rate * i, 4) for i in range(n))


# ── schedule pressure ─────────────────────────────────────────────────────────────────
def test_schedule_pressure_low_when_on_pace_high_when_behind():
    # required rate = work_remaining/days_remaining = 0.5/20 = 0.025/day
    on_pace = ProjectSignals("m", days_remaining=20, work_remaining=0.5, progress_series=_steady(0.025))
    behind = ProjectSignals("m", days_remaining=20, work_remaining=0.5, progress_series=_steady(0.010))
    assert schedule_pressure(on_pace) < 0.15
    assert schedule_pressure(behind) > 0.5


def test_schedule_pressure_maxes_when_no_time_left_with_work():
    s = ProjectSignals("m", days_remaining=0.0, work_remaining=0.4, progress_series=_steady(0.02))
    assert schedule_pressure(s) == 1.0


def test_progress_deceleration_flags_slowing_not_accelerating():
    decel = ProjectSignals("m", progress_series=(0.1, 0.35, 0.55, 0.68, 0.75, 0.78))   # deltas shrink
    accel = ProjectSignals("m", progress_series=(0.1, 0.13, 0.18, 0.27, 0.45, 0.75))   # deltas grow
    assert progress_deceleration(decel) > progress_deceleration(accel)
    assert progress_deceleration(accel) == 0.0


# ── dependency / blocked / decision debt ─────────────────────────────────────────────
def test_dependency_lag_takes_the_worst_dependency():
    s = ProjectSignals("m", dependency_lags=(0.2, 0.9, 0.5))
    assert dependency_lag(s) == pytest.approx(0.9)


def test_blocked_scales_and_caps():
    assert blocked_dependencies(ProjectSignals("m", blocked_dependencies=0)) == 0.0
    assert blocked_dependencies(ProjectSignals("m", blocked_dependencies=3)) == pytest.approx(1.0)
    assert blocked_dependencies(ProjectSignals("m", blocked_dependencies=9)) == 1.0   # capped


def test_decision_debt_weighted_by_age():
    fresh = ProjectSignals("m", open_critical_decisions=1, oldest_decision_age_days=1.0)
    stale = ProjectSignals("m", open_critical_decisions=1, oldest_decision_age_days=14.0)
    assert decision_debt(stale) > decision_debt(fresh)
    assert decision_debt(ProjectSignals("m", open_critical_decisions=0)) == 0.0


def test_stale_approval_and_test_insufficiency():
    assert stale_approval(ProjectSignals("m", approval_pending=False, approval_age_days=30)) == 0.0
    assert stale_approval(ProjectSignals("m", approval_pending=True, approval_age_days=7)) == pytest.approx(1.0)
    # under-testing bites harder as the milestone nears
    near = ProjectSignals("m", days_remaining=2, work_remaining=0.3, test_coverage=0.2)
    far = ProjectSignals("m", days_remaining=60, work_remaining=0.3, test_coverage=0.2)
    assert under_testing(near) > under_testing(far)


# ── ensemble / cause / window ────────────────────────────────────────────────────────
def test_raw_risk_bounded_and_higher_for_multi_signal_risk():
    calm = ProjectSignals("m", days_remaining=30, work_remaining=0.4, progress_series=_steady(0.02))
    risky = ProjectSignals("m", days_remaining=15, work_remaining=0.6, progress_series=_steady(0.01),
                           dependency_lags=(0.9,), blocked_dependencies=2,
                           open_critical_decisions=1, oldest_decision_age_days=10)
    assert 0.0 <= raw_risk(calm) <= 1.0 and 0.0 <= raw_risk(risky) <= 1.0
    assert raw_risk(risky) > raw_risk(calm)


def test_signal_vector_has_all_weighted_signals():
    from agentic_os.execution_risk import DEFAULT_WEIGHTS
    assert set(signal_vector(ProjectSignals("m"))) == set(DEFAULT_WEIGHTS)


def test_primary_cause_names_the_dominant_dependency_risk():
    s = ProjectSignals("m", days_remaining=25, work_remaining=0.5, progress_series=_steady(0.02),
                       dependency_lags=(0.95,))
    assert "dependency" in primary_cause(s).lower()


def test_slip_window_zero_on_track_positive_when_behind():
    on_track = ProjectSignals("m", days_remaining=20, work_remaining=0.4, progress_series=_steady(0.03))
    behind = ProjectSignals("m", days_remaining=10, work_remaining=0.6, progress_series=_steady(0.01))
    assert slip_window_days(on_track) == (0.0, 0.0)
    lo, hi = slip_window_days(behind)
    assert hi > lo > 0


# ── calibrator reuse ──────────────────────────────────────────────────────────────────
def test_calibrator_is_monotone():
    cal = IsotonicCalibrator().fit([0.1, 0.2, 0.3, 0.5, 0.7, 0.9], [0, 0, 0, 1, 1, 1])
    ys = [cal.transform(x / 100) for x in range(101)]
    assert all(b >= a - 1e-9 for a, b in zip(ys, ys[1:]))


# ── assess: abstention gates + levels ────────────────────────────────────────────────
def test_assess_abstains_on_thin_evidence():
    r = assess(ProjectSignals("m", progress_series=(0.1, 0.2)), mode=Mode.EXPLORATORY)  # <3 points
    assert r.abstained and any("thin" in x for x in r.reasons)


def test_assess_abstains_when_complete():
    r = assess(ProjectSignals("m", work_remaining=0.0, progress_series=_steady(0.02)), mode=Mode.EXPLORATORY)
    assert r.abstained and any("complete" in x for x in r.reasons)


def test_assess_abstains_below_threshold_but_surfaces_strong_risk():
    healthy = ProjectSignals("m", days_remaining=30, work_remaining=0.3, progress_series=_steady(0.02))
    assert assess(healthy, mode=Mode.STRICT).abstained
    # the radar runs WITH a calibrator (raw scores are compressed; calibration maps them to slip
    # probabilities). A calibrator trained so the risky region reads high lets a strong risk surface.
    risky = ProjectSignals("m", days_remaining=12, work_remaining=0.7, progress_series=_steady(0.008),
                           dependency_lags=(0.9,), blocked_dependencies=2,
                           open_critical_decisions=1, oldest_decision_age_days=12)
    cal = IsotonicCalibrator().fit([0.05, 0.1, 0.2, raw_risk(risky), 0.5], [0, 0, 0, 1, 1])
    r = assess(risky, calibrator=cal, mode=Mode.EXPLORATORY)
    assert not r.abstained and r.risk_level in (RiskLevel.ELEVATED, RiskLevel.CRITICAL)
    assert r.slip_window_days[1] > 0 and r.primary_cause


def test_assess_calibrated_confidence_differs_from_raw():
    cal = IsotonicCalibrator().fit([0.05, 0.1, 0.4, 0.5], [0, 0, 1, 1])
    s = ProjectSignals("m", days_remaining=15, work_remaining=0.6, progress_series=_steady(0.01),
                       dependency_lags=(0.8,))
    r = assess(s, calibrator=cal, mode=Mode.EXPLORATORY)
    assert r.raw_score == pytest.approx(raw_risk(s))
    assert r.confidence == pytest.approx(cal.transform(r.raw_score))


# ── the Priority Engine adapter ───────────────────────────────────────────────────────
def test_risk_adapter_respects_abstain_and_builds_an_approval_gated_candidate():
    from agentic_os.agent_gateway.contracts import RiskTier
    from agentic_os.priority_engine import decide, Action, from_risk_report
    healthy = assess(ProjectSignals("m", days_remaining=30, work_remaining=0.3,
                                    progress_series=_steady(0.02)), mode=Mode.STRICT)
    assert from_risk_report(healthy) is None                 # abstained ⇒ no candidate
    signals = ProjectSignals("Production pilot", days_remaining=12, work_remaining=0.7,
                             progress_series=_steady(0.008), dependency_lags=(0.9,),
                             blocked_dependencies=2, open_critical_decisions=1,
                             oldest_decision_age_days=12)
    cal = IsotonicCalibrator().fit([0.05, 0.1, 0.2, raw_risk(signals), 0.5], [0, 0, 0, 1, 1])
    risky = assess(signals, calibrator=cal, mode=Mode.EXPLORATORY)
    c = from_risk_report(risky)
    assert c is not None and "Production pilot" in c.subject
    assert c.risk_tier == RiskTier.CONSEQUENTIAL
    assert c.expected_value == pytest.approx(risky.confidence)
    assert decide(c).action == Action.REQUEST_APPROVAL
