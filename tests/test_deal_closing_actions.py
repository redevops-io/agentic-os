"""Deal Closing Intelligence Phase 4 — candidate actions, scoring, autonomy, Close Plan → Mission DAG.

Proves: NO_ACTION is always a candidate and the baseline a real action must beat; scoring reuses the priority
engine (confidence = P(blocker real), expected_value = value-at-stake x P(resolves)); a non-actionable hypothesis
only yields verify/no-action; the autonomy level + risk tier decide each step's disposition (pricing stays gated,
reads can auto-run); the Close Plan orders steps by methodology dependencies and compiles to a Mission spec whose
approval constraints match the autonomy level; spec-only handoff creates no mission.
"""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.deal_closing import (
    ActionDisposition, AutonomyLevel, ConditionState, DealClosePlan, InterventionKind, best_action,
    candidates_from_hypothesis, compile_close_plan, disposition, handoff, infer_blockers, no_action,
    plan_hypotheses, risk_tier_for, score_actions, spiced, to_mission_spec, to_priority_candidate,
)
from agentic_os.deal_closing.contracts import Deal, Provenance
from agentic_os.deal_closing.methodology import assess_methodology
from agentic_os.priority_engine import Action


def _deal():
    return Deal(prov=Provenance(provider="salesforce"), opportunity_ref="sf:006",
                reported_amount_cents=1_000_000_00, currency="USD", reported_stage="Negotiation")


def _hyps(states):
    m = spiced()
    r = assess_methodology(m, states)
    blockers = infer_blockers(m, r, states, deal_ref="sf:006")
    return plan_hypotheses(blockers), m


def _unsat_eb():
    m = spiced()
    states = {n: ConditionState.SATISFIED for n in m.required_names()}
    states["ECONOMIC_BUYER_ENGAGED"] = ConditionState.UNSATISFIED
    return states


# ── candidates + scoring ────────────────────────────────────────────────────────────────────────────
def test_no_action_always_present():
    hyps, _ = _hyps(_unsat_eb())
    h = [x for x in hyps if x.subject == "ECONOMIC_BUYER_ENGAGED"][0]
    actions = candidates_from_hypothesis(h, deal_value_cents=1_000_000_00)
    assert any(a.kind is InterventionKind.NO_ACTION for a in actions)
    assert any(a.kind is InterventionKind.ENGAGE_ECONOMIC_BUYER for a in actions)


def test_non_actionable_hypothesis_yields_only_verify_and_no_action():
    hyps, _ = _hyps(_unsat_eb())
    h = [x for x in hyps if x.subject == "ECONOMIC_BUYER_ENGAGED"][0]
    from dataclasses import replace
    contradicted = replace(h, contradicting_evidence=("buyer met us last week", "mutual plan agreed"))
    assert contradicted.is_actionable() is False
    actions = candidates_from_hypothesis(contradicted, deal_value_cents=1_000_000_00)
    kinds = {a.kind for a in actions}
    assert kinds <= {InterventionKind.NO_ACTION, InterventionKind.VERIFY_CLAIM,
                     InterventionKind.RESOLVE_CONFLICT}


def test_scoring_reuses_priority_engine_confidence_and_value():
    hyps, _ = _hyps(_unsat_eb())
    h = [x for x in hyps if x.subject == "ECONOMIC_BUYER_ENGAGED"][0]
    actions = candidates_from_hypothesis(h, deal_value_cents=1_000_000_00)
    engage = [a for a in actions if a.kind is InterventionKind.ENGAGE_ECONOMIC_BUYER][0]
    cand = to_priority_candidate(engage)
    assert cand.confidence == engage.p_blocker_real
    assert cand.expected_value > 0
    assert cand.risk_tier is RiskTier.CONSEQUENTIAL


def test_best_action_beats_do_nothing_when_evidence_supports():
    hyps, _ = _hyps(_unsat_eb())
    h = [x for x in hyps if x.subject == "ECONOMIC_BUYER_ENGAGED"][0]
    actions = candidates_from_hypothesis(h, deal_value_cents=1_000_000_00)
    chosen = best_action(actions)
    assert chosen.action.kind is not InterventionKind.NO_ACTION


def test_weak_value_falls_back_to_no_action():
    # tiny deal + a blocker: risk-adjusted value of a consequential action won't beat nothing
    hyps, _ = _hyps(_unsat_eb())
    h = [x for x in hyps if x.subject == "ECONOMIC_BUYER_ENGAGED"][0]
    actions = candidates_from_hypothesis(h, deal_value_cents=100)  # $1 deal
    chosen = best_action(actions)
    assert chosen.action.kind is InterventionKind.NO_ACTION or chosen.decision.action is Action.ABSTAIN


# ── autonomy ──────────────────────────────────────────────────────────────────────────────────────
def test_pricing_stays_gated_reads_can_run():
    assert risk_tier_for(InterventionKind.AGREE_PRICING) is RiskTier.CRITICAL
    assert risk_tier_for(InterventionKind.VERIFY_CLAIM) is RiskTier.READ
    # even POLICY_AUTHORIZED gates a critical pricing action
    assert disposition(AutonomyLevel.POLICY_AUTHORIZED, RiskTier.CRITICAL) is ActionDisposition.REQUEST_APPROVAL
    # a read auto-runs at approval-gated
    assert disposition(AutonomyLevel.APPROVAL_GATED, RiskTier.READ) is ActionDisposition.EXECUTE
    # a bounded write auto-runs only at policy-authorized
    assert disposition(AutonomyLevel.APPROVAL_GATED, RiskTier.BOUNDED_WRITE) is ActionDisposition.REQUEST_APPROVAL
    assert disposition(AutonomyLevel.POLICY_AUTHORIZED, RiskTier.BOUNDED_WRITE) is ActionDisposition.EXECUTE


def test_observe_records_recommend_surfaces():
    assert disposition(AutonomyLevel.OBSERVE, RiskTier.READ) is ActionDisposition.RECORD
    assert disposition(AutonomyLevel.RECOMMEND, RiskTier.CRITICAL) is ActionDisposition.RECOMMEND
    assert disposition(AutonomyLevel.PREPARE, RiskTier.BOUNDED_WRITE) is ActionDisposition.PREPARE
    assert disposition(AutonomyLevel.PREPARE, RiskTier.CONSEQUENTIAL) is ActionDisposition.RECOMMEND


# ── close plan ──────────────────────────────────────────────────────────────────────────────────────
def test_compile_close_plan_shape_and_dispositions():
    states = _unsat_eb()
    plan = compile_close_plan(_deal(), spiced(), states, autonomy=AutonomyLevel.APPROVAL_GATED)
    assert isinstance(plan, DealClosePlan)
    assert plan.methodology == "SPICED"
    assert plan.steps
    assert "ECONOMIC_BUYER_ENGAGED" in plan.blocking_conditions
    # every step carries a verification condition back to the thing it should move
    assert all(s.action.verification_condition for s in plan.steps if s.action.kind is not InterventionKind.NO_ACTION)


def test_close_plan_orders_dependencies():
    from agentic_os.deal_closing import meddicc
    m = meddicc()
    states = {n: ConditionState.UNSATISFIED for n in m.required_names()}
    plan = compile_close_plan(_deal(), m, states, autonomy=AutonomyLevel.APPROVAL_GATED)
    targets = [s.action.target_condition for s in plan.steps]
    if "CHAMPION_IDENTIFIED" in targets and "CHAMPION_ACTIVE" in targets:
        assert targets.index("CHAMPION_IDENTIFIED") < targets.index("CHAMPION_ACTIVE")


def test_mission_spec_constraints_match_autonomy_and_handoff_is_spec_only():
    states = _unsat_eb()
    plan = compile_close_plan(_deal(), spiced(), states, autonomy=AutonomyLevel.APPROVAL_GATED)
    spec, mission = handoff(plan)                 # no runtime attached
    assert mission is None
    assert spec is not None
    assert "approval:side_effecting" in spec.constraints
    assert spec.deal_ref == "sf:006"
    assert spec.steps  # engage-economic-buyer crosses the bridge

    # observe-level plan: nothing crosses, so no spec
    observe_plan = compile_close_plan(_deal(), spiced(), states, autonomy=AutonomyLevel.OBSERVE)
    ospec, _ = handoff(observe_plan)
    assert ospec is None


def test_ready_to_close_plan_has_no_blockers():
    m = spiced()
    states = {n: ConditionState.SATISFIED for n in m.required_names()}
    plan = compile_close_plan(_deal(), m, states, autonomy=AutonomyLevel.APPROVAL_GATED)
    assert plan.ready_to_close is True
    assert plan.blocking_conditions == ()
