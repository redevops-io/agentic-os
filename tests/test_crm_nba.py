"""Tests for the CRM Next-Best-Action producer (agentic_os.crm_nba). It produces well-formed
DecisionOpportunities and maps commercial outcomes into the shared OutcomeEvent shape; selection and
learning belong to the shared runtime. The integration test shows the loop correcting CRM's priors."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.crm_nba import DealSignals, crm_outcome, next_best_action
from agentic_os.outcome_learning import UtilityModel
from agentic_os.priority_engine import Action, OutcomeEvent, OutcomeLog, select_action


def _kinds(opp):
    return {c.action_kind for c in opp.candidate_actions}


def test_opportunity_offers_stage_appropriate_actions():
    buying = next_best_action(DealSignals("Acme", stage="engaged", buying_signal=True))
    assert "send_proposal" in _kinds(buying)
    q = next_best_action(DealSignals("Beta", open_technical_question=True))
    assert "answer_question" in _kinds(q)
    renewal = next_best_action(DealSignals("Gamma", renewal_days=30))
    assert "renewal_outreach" in _kinds(renewal)


def test_outbound_actions_are_consequential_and_monitor_is_read():
    opp = next_best_action(DealSignals("Acme", stage="engaged", buying_signal=True))
    by = {c.action_kind: c for c in opp.candidate_actions}
    assert by["send_proposal"].risk_tier == RiskTier.CONSEQUENTIAL
    assert by["monitor"].risk_tier == RiskTier.READ
    assert opp.constraints["max_risk_tier"] == RiskTier.CONSEQUENTIAL


def test_selecting_an_outbound_action_routes_to_approval():
    opp = next_best_action(DealSignals("Acme", stage="engaged", buying_signal=True))
    sel = select_action(opp)
    if sel.action.risk_tier == RiskTier.CONSEQUENTIAL:
        assert sel.decision.action == Action.REQUEST_APPROVAL


def test_crm_outcome_shape_rewards_conversion_penalises_loss_and_discounts_delay():
    opp = next_best_action(DealSignals("Acme", stage="engaged", buying_signal=True))
    send = next(c for c in opp.candidate_actions if c.action_kind == "send_proposal")
    won = crm_outcome(send, replied=True, meeting=True, converted=True, delay_hours=12)
    lost = crm_outcome(send, replied=True, lost=True, delay_hours=12)
    assert won.scalar_reward() > 0 and lost.scalar_reward() < won.scalar_reward()
    assert lost.reward_dimensions.get("loss") == -1.0
    near = crm_outcome(send, converted=True, delay_hours=1)
    far = crm_outcome(send, converted=True, delay_hours=1000)
    assert near.attribution_confidence > far.attribution_confidence     # delayed ⇒ less confidently attributed


def test_loop_corrects_the_crm_prior_from_outcomes():
    # CRM's prior favours send_proposal; but if the OUTCOMES show schedule_call converts and
    # send_proposal loses, the shared learner shifts the selection to schedule_call.
    opp = next_best_action(DealSignals("Acme", stage="engaged", buying_signal=True))
    assert select_action(opp).action.action_kind == "send_proposal"      # static: prior wins
    by = {c.action_kind: c for c in opp.candidate_actions}
    log = OutcomeLog()
    for _ in range(30):
        log.record(crm_outcome(by["send_proposal"], replied=True, lost=True, delay_hours=48))
        log.record(crm_outcome(by["schedule_call"], meeting=True, converted=True, delay_hours=48))
    learned = select_action(opp, utility_fn=UtilityModel().fit(log).as_utility_fn())
    assert learned.action.action_kind == "schedule_call"
