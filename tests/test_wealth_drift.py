"""Tests for the Wealth Assumption-Drift producer (agentic_os.wealth_drift): the drift gate, review-not-
trade candidate set, downside-aware outcome mapping, and the loop learning to flag only genuine drift."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.outcome_learning import UtilityModel
from agentic_os.priority_engine import Action, OutcomeLog, select_action
from agentic_os.wealth_drift import PlanAssumption, drift, drift_opportunity, is_material, wealth_outcome


def _kinds(opp):
    return {c.action_kind for c in opp.candidate_actions}


def test_drift_gate():
    drifted = PlanAssumption("P", "inflation ~2%", assumed_value=2.0, current_value=3.6, tolerance=0.8)
    steady = PlanAssumption("P", "inflation ~2%", assumed_value=2.0, current_value=2.3, tolerance=0.8)
    assert is_material(drifted) and not is_material(steady)
    assert drift(drifted) == pytest.approx(2.0)                 # 1.6 / 0.8


def test_within_tolerance_only_monitors():
    opp = drift_opportunity(PlanAssumption("P", "inflation ~2%", 2.0, 2.3, 0.8))
    assert _kinds(opp) == {"monitor"}


def test_material_drift_offers_review_not_a_trade():
    opp = drift_opportunity(PlanAssumption("P", "inflation ~2%", 2.0, 3.6, 0.8))
    ks = _kinds(opp)
    assert "flag_for_review" in ks and "rebalance_proposal" in ks
    by = {c.action_kind: c for c in opp.candidate_actions}
    # a review flag is a reversible bounded write; a rebalance is only a PROPOSAL that parks on approval
    assert by["flag_for_review"].risk_tier == RiskTier.BOUNDED_WRITE
    assert by["rebalance_proposal"].risk_tier == RiskTier.CONSEQUENTIAL
    # never a CRITICAL (trade-executing) action, by construction of the constraint
    assert opp.constraints["max_risk_tier"] == RiskTier.CONSEQUENTIAL
    assert all(c.risk_tier != RiskTier.CRITICAL for c in opp.candidate_actions)


def test_outcome_rewards_confirmed_drift_penalises_needless_flag():
    opp = drift_opportunity(PlanAssumption("P", "inflation ~2%", 2.0, 3.6, 0.8))
    flag = next(c for c in opp.candidate_actions if c.action_kind == "flag_for_review")
    assert wealth_outcome(flag, assumption_confirmed_stale=True, plan_improved=True).scalar_reward() > 0
    assert wealth_outcome(flag, needless_flag=True).scalar_reward() < 0


def test_loop_learns_to_stop_flagging_when_it_is_just_nagging():
    opp = drift_opportunity(PlanAssumption("P", "inflation ~2%", 2.0, 3.6, 0.8))
    assert select_action(opp).action.action_kind == "flag_for_review"   # prior: flag it
    by = {c.action_kind: c for c in opp.candidate_actions}
    log = OutcomeLog()
    for _ in range(40):
        log.record(wealth_outcome(by["flag_for_review"], needless_flag=True))
        log.record(wealth_outcome(by["rebalance_proposal"], needless_flag=True))
    learned = select_action(opp, utility_fn=UtilityModel().fit(log).as_utility_fn())
    assert learned.action.action_kind == "monitor"              # learned not to nag → no outbound review
