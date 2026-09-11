"""Tests for the common decision/outcome contract (agentic_os.priority_engine): DecisionOpportunity,
select_action, the enriched OutcomeEvent, and OutcomeLog. These test the SELECTION + contract mechanics
(governance-preserving, do-nothing first-class, constraint-aware, explainable). Learning is tested in
test_outcome_learning.py.
"""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import (
    Action, DecisionOpportunity, InterventionCandidate, OutcomeEvent, OutcomeLog, SelectedAction,
    record_outcome, select_action)


def _cand(kind, ev, conf=0.85, tier=RiskTier.BOUNDED_WRITE, **kw):
    return InterventionCandidate(source_app="crm", subject="Acme", proposed_action=f"do {kind}",
                                 expected_value=ev, confidence=conf, action_kind=kind, risk_tier=tier,
                                 candidate_id=f"crm:{kind}", **kw)


def _opp(*cands, **kw):
    return DecisionOpportunity(entity="Acme", source_app="crm", candidate_actions=tuple(cands),
                              opportunity_id="o1", **kw)


# ── select_action ─────────────────────────────────────────────────────────────────────
def test_select_picks_highest_expected_utility():
    opp = _opp(_cand("small", 0.2), _cand("big", 0.9), _cand("mid", 0.5))
    sel = select_action(opp)
    assert isinstance(sel, SelectedAction) and sel.action.action_kind == "big"
    assert round(sel.expected_utility, 3) == max(u for _, u in sel.alternatives)   # top of the ranked alts


def test_do_nothing_is_always_in_the_pool_and_wins_when_actions_are_weak():
    # every candidate is net-negative ⇒ do-nothing (utility 0) wins ⇒ ABSTAIN (§17)
    opp = _opp(_cand("a", -0.3), _cand("b", -0.5))
    sel = select_action(opp)
    assert sel.decision.action == Action.ABSTAIN


def test_selection_preserves_governance_routing():
    opp = _opp(_cand("send", 0.9, tier=RiskTier.CONSEQUENTIAL))
    sel = select_action(opp)
    assert sel.action.action_kind == "send" and sel.decision.action == Action.REQUEST_APPROVAL


def test_constraint_filters_out_too_risky_actions():
    opp = _opp(_cand("safe", 0.5, tier=RiskTier.BOUNDED_WRITE),
               _cand("risky", 0.95, tier=RiskTier.CRITICAL),
               constraints={"max_risk_tier": RiskTier.BOUNDED_WRITE})
    sel = select_action(opp)
    assert sel.action.action_kind == "safe"                # the higher-value CRITICAL action is excluded


def test_selection_is_explainable_and_deterministic():
    opp = _opp(_cand("a", 0.6), _cand("b", 0.8))
    s1 = select_action(opp)
    s2 = select_action(opp)
    assert s1.reason and s1.alternatives and s1 == s2       # explainable + deterministic


def test_utility_fn_can_override_the_static_prior():
    # 'weak' has the higher prior, but a utility_fn that loves 'strong' flips the choice
    opp = _opp(_cand("weak", 0.9), _cand("strong", 0.3))
    fn = lambda c, base: base + (5.0 if c.action_kind == "strong" else 0.0)
    assert select_action(opp).action.action_kind == "weak"
    assert select_action(opp, utility_fn=fn).action.action_kind == "strong"


# ── enriched OutcomeEvent ─────────────────────────────────────────────────────────────
def test_scalar_reward_prefers_explicit_then_dimensions_scaled_by_attribution():
    assert OutcomeEvent("c", "crm", Action.ACT, observed_reward=0.5).scalar_reward() == pytest.approx(0.5)
    ev = OutcomeEvent("c", "crm", Action.ACT, reward_dimensions={"reply": 1.0, "unsub": -1.0},
                      attribution_confidence=0.5)
    # weighted sum (reply 1 - unsub 1 = 0 by default weights)… use non-trivial weights
    ev2 = OutcomeEvent("c", "crm", Action.ACT, reward_dimensions={"reply": 1.0}, attribution_confidence=0.5)
    assert ev2.scalar_reward() == pytest.approx(0.5)        # 1.0 * attribution 0.5
    assert ev2.scalar_reward({"reply": 2.0}) == pytest.approx(1.0)   # weight 2 * 1.0 * 0.5


def test_record_outcome_carries_the_action_kind_and_reward_shape():
    opp = _opp(_cand("send", 0.9, tier=RiskTier.CONSEQUENTIAL))
    sel = select_action(opp)
    ev = record_outcome(sel.decision, reward_dimensions={"meeting": 1.0}, delay=48.0,
                        attribution_confidence=0.7)
    assert ev.action_kind == "send" and ev.delay == 48.0 and ev.reward_dimensions == {"meeting": 1.0}
    assert ev.scalar_reward() == pytest.approx(0.7)


# ── OutcomeLog ────────────────────────────────────────────────────────────────────────
def test_outcome_log_records_and_filters_by_key():
    log = OutcomeLog()
    log.record(OutcomeEvent("c1", "crm", Action.ACT, action_kind="send", observed_reward=1.0))
    log.record(OutcomeEvent("c2", "crm", Action.ACT, action_kind="wait", observed_reward=0.0))
    log.record(OutcomeEvent("c3", "crm", Action.ACT, action_kind="send", observed_reward=0.5))
    assert len(log.events) == 3
    assert len(log.for_key("crm", "send")) == 2 and len(log.for_key("crm", "nope")) == 0
