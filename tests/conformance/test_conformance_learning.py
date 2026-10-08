"""N5 conformance (plan §9): a real outcome changes WHICH action is favoured, and only a promoted
policy does so. Pure — no runtime needed."""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import ApprovalPolicy, RiskTier
from agentic_os.outcome_learning import UtilityModel
from agentic_os.policy_promotion import STATIC_POLICY, PolicyRegistry, build_candidate
from agentic_os.priority_engine import (
    DecisionOpportunity,
    InterventionCandidate,
    OutcomeEvent,
    OutcomeLog,
    select_action,
)


def _cand(kind, ev):
    return InterventionCandidate(
        source_app="ref", subject="S", proposed_action=kind, expected_value=ev, confidence=0.9,
        risk_tier=RiskTier.READ, approval_policy=ApprovalPolicy.AUTO,
        required_capabilities=(f"ref.{kind}",), candidate_id=f"c-{kind}", action_kind=kind)


def _opp():
    # 'a' has the higher static prior, so static selection prefers it.
    return DecisionOpportunity(entity="S", source_app="ref",
                               candidate_actions=(_cand("a", 1.0), _cand("b", 0.9)),
                               opportunity_id="opp")


def _log_favouring_b():
    log = OutcomeLog()
    for _ in range(20):
        log.record(OutcomeEvent(candidate_id="c-a", source_app="ref", action_kind="a",
                                observed_reward=-1.0, attribution_confidence=0.9))
        log.record(OutcomeEvent(candidate_id="c-b", source_app="ref", action_kind="b",
                                observed_reward=2.0, attribution_confidence=0.9))
    return log


def test_static_selection_prefers_the_higher_prior():
    sel = select_action(_opp())
    assert sel.action.action_kind == "a"          # no learning: the prior wins


def test_learned_utility_changes_the_favoured_action():
    model = UtilityModel().fit(_log_favouring_b())
    sel = select_action(_opp(), utility_fn=model.as_utility_fn())
    assert sel.action.action_kind == "b"          # the real outcomes flipped the choice


def test_selection_only_changes_through_a_promoted_policy():
    opp = _opp()
    registry = PolicyRegistry(active=STATIC_POLICY, history=[])
    # the active (static) policy carries no learned utility -> selection is still the prior
    sel_before = select_action(opp, utility_fn=registry.active.utility_fn())
    assert sel_before.action.action_kind == "a"

    candidate = build_candidate(_log_favouring_b().events)
    registry.promote(candidate)                   # promotion is the ONLY way the policy changes
    sel_after = select_action(opp, utility_fn=registry.active.utility_fn())
    assert sel_after.action.action_kind == "b"
    assert registry.active.kind == "learned"
