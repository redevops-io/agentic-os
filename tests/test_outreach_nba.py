"""Tests for the Outreach Intent & Timing producer (agentic_os.outreach_nba). Well-formed
opportunities, downside-aware outcome mapping, opt-out safety, and the loop learning to stop
contacting when outreach draws negative outcomes. (Distinct from tests/test_outreach.py, which covers
the separate integrations.outreach activation Mission.)"""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.outcome_learning import UtilityModel
from agentic_os.outreach_nba import ProspectSignals, outreach_opportunity, outreach_outcome
from agentic_os.priority_engine import Action, OutcomeLog, select_action


def _kinds(opp):
    return {c.action_kind for c in opp.candidate_actions}


def test_opted_out_prospect_offers_no_outbound_action():
    opp = outreach_opportunity(ProspectSignals("X", opted_out=True))
    assert _kinds(opp) == {"monitor"}                       # can never select an outbound action
    assert select_action(opp).decision.action in (Action.ACT, Action.ABSTAIN)   # monitor/do-nothing only


def test_fresh_trigger_raises_the_email_prior_and_urgency():
    hot = outreach_opportunity(ProspectSignals("X", fresh_trigger=True))
    cold = outreach_opportunity(ProspectSignals("Y", fresh_trigger=False, prior_no_response_streak=3))
    hot_email = next(c for c in hot.candidate_actions if c.action_kind == "email_now")
    cold_email = next(c for c in cold.candidate_actions if c.action_kind == "email_now")
    assert hot_email.expected_value > cold_email.expected_value and hot_email.urgency > cold_email.urgency


def test_outreach_outcome_counts_the_downside():
    opp = outreach_opportunity(ProspectSignals("X", fresh_trigger=True))
    email = next(c for c in opp.candidate_actions if c.action_kind == "email_now")
    good = outreach_outcome(email, positive_reply=True, meeting=True)
    bad = outreach_outcome(email, unsubscribed=True)
    assert good.scalar_reward() > 0 > bad.scalar_reward()
    assert bad.reward_dimensions.get("unsubscribe") == -1.0


def test_loop_learns_to_stop_contacting_when_outreach_backfires():
    # email_now has a decent prior, but if outcomes are dominated by unsubscribes/negative replies,
    # the learner drives its utility negative so the loop prefers waiting/monitoring (no outbound).
    opp = outreach_opportunity(ProspectSignals("X", fresh_trigger=True))
    assert select_action(opp).action.action_kind == "email_now"          # static: prior contacts now
    by = {c.action_kind: c for c in opp.candidate_actions}
    log = OutcomeLog()
    for _ in range(40):
        log.record(outreach_outcome(by["email_now"], unsubscribed=True))
        log.record(outreach_outcome(by["linkedin_touch"], negative_reply=True))
    learned = select_action(opp, utility_fn=UtilityModel().fit(log).as_utility_fn())
    assert learned.action.action_kind in ("wait_for_trigger", "monitor")  # stopped the backfiring outreach
    assert learned.action.risk_tier == RiskTier.READ                      # a non-outbound choice
