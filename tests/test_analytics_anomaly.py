"""Tests for the Analytics Anomaly→Action producer (agentic_os.analytics_anomaly): the materiality
gate, stage-appropriate candidate actions, downside-aware outcome mapping, and the shared loop
correcting its prior."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.analytics_anomaly import (
    MetricAnomaly, analytics_outcome, anomaly_opportunity, is_adverse, is_material, zscore)
from agentic_os.outcome_learning import UtilityModel
from agentic_os.priority_engine import Action, OutcomeLog, select_action


def _kinds(opp):
    return {c.action_kind for c in opp.candidate_actions}


def test_materiality_gate_and_direction():
    big_drop = MetricAnomaly("activation", observed=0.30, baseline_mean=0.52, baseline_std=0.04)
    wobble = MetricAnomaly("activation", observed=0.51, baseline_mean=0.52, baseline_std=0.04)
    assert is_material(big_drop) and not is_material(wobble)
    assert is_adverse(big_drop)                                  # a drop in a higher-is-better metric
    assert not is_adverse(MetricAnomaly("errors", 0.2, 0.5, 0.05, higher_is_better=False))  # a drop in a bad metric


def test_immaterial_anomaly_only_offers_monitor():
    opp = anomaly_opportunity(MetricAnomaly("activation", 0.51, 0.52, 0.04))
    assert _kinds(opp) == {"monitor"}                            # can't be pushed to act on noise


def test_material_adverse_anomaly_offers_governed_responses():
    opp = anomaly_opportunity(MetricAnomaly("activation", 0.30, 0.52, 0.04))
    ks = _kinds(opp)
    assert {"investigate_cause", "roll_back_change", "adjust_lever"} <= ks
    by = {c.action_kind: c for c in opp.candidate_actions}
    assert by["investigate_cause"].risk_tier == RiskTier.READ            # analysis is low-risk
    assert by["roll_back_change"].risk_tier == RiskTier.CONSEQUENTIAL    # a live change needs approval


def test_outcome_rewards_recovery_penalises_false_alarm():
    opp = anomaly_opportunity(MetricAnomaly("activation", 0.30, 0.52, 0.04))
    roll = next(c for c in opp.candidate_actions if c.action_kind == "roll_back_change")
    assert analytics_outcome(roll, cause_found=True, metric_recovered=True).scalar_reward() > 0
    assert analytics_outcome(roll, false_alarm=True).scalar_reward() < 0


def test_loop_corrects_the_analytics_prior():
    opp = anomaly_opportunity(MetricAnomaly("activation", 0.30, 0.52, 0.04))
    before = select_action(opp).action.action_kind
    by = {c.action_kind: c for c in opp.candidate_actions}
    log = OutcomeLog()
    for _ in range(30):
        # the chosen prior action turns out to be a false alarm; adjust_lever actually recovers it
        log.record(analytics_outcome(by[before], false_alarm=True))
        log.record(analytics_outcome(by["adjust_lever"], cause_found=True, metric_recovered=True))
    after = select_action(opp, utility_fn=UtilityModel().fit(log).as_utility_fn()).action.action_kind
    assert after == "adjust_lever" and after != before
