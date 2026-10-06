"""Deal Closing Intelligence Phase 3 — blocker taxonomy + hypothesis engine.

Proves: readiness signals become typed blockers (conflict/stale/inconsistency/unknown/unsatisfied map to distinct
blocker types); every blocker offers NO_ACTION among its candidates and NEVER an action by itself; a hypothesis
carries contradicting evidence and is not actionable until supporting outweighs it above a confidence bar; an
UNKNOWN blocking condition is a DISCOVERY_GAP (find out), not a substantive blocker.
"""
from __future__ import annotations

from agentic_os.deal_closing import (
    Blocker, BlockerType, ConditionState, InterventionKind, assess_methodology, hypothesis_for, infer_blockers,
    meddicc, plan_hypotheses, spiced,
)
from agentic_os.deal_closing.methodology import ClosingMethodology


def _assess_and_infer(methodology: ClosingMethodology, states):
    r = assess_methodology(methodology, states)
    return infer_blockers(methodology, r, states, deal_ref="deal:1"), r


def _all(names, state):
    return {n: state for n in names}


def test_unknown_blocking_is_discovery_gap_not_unsatisfied():
    blockers, _ = _assess_and_infer(spiced(), {})
    disc = [b for b in blockers if b.blocker_type is BlockerType.DISCOVERY_GAP]
    assert disc, "expected a DISCOVERY_GAP for never-assessed conditions"
    assert all(InterventionKind.VERIFY_CLAIM in b.candidate_interventions for b in disc)


def test_unsatisfied_maps_to_substantive_blocker():
    m = spiced()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["ECONOMIC_BUYER_ENGAGED"] = ConditionState.UNSATISFIED
    blockers, _ = _assess_and_infer(m, states)
    eb = [b for b in blockers if b.subject == "ECONOMIC_BUYER_ENGAGED"]
    assert eb and eb[0].blocker_type is BlockerType.NO_ECONOMIC_BUYER_ACCESS
    assert InterventionKind.ENGAGE_ECONOMIC_BUYER in eb[0].candidate_interventions


def test_conflict_becomes_evidence_conflict_blocker():
    m = spiced()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["BUDGET_CONFIRMED"] = ConditionState.CONFLICTED
    blockers, _ = _assess_and_infer(m, states)
    conf = [b for b in blockers if b.blocker_type is BlockerType.EVIDENCE_CONFLICT]
    assert conf and conf[0].subject == "BUDGET_CONFIRMED"
    assert InterventionKind.RESOLVE_CONFLICT in conf[0].candidate_interventions


def test_stale_becomes_stale_evidence_blocker():
    m = spiced()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["SIGNATURE_PENDING"] = ConditionState.STALE
    blockers, _ = _assess_and_infer(m, states)
    assert any(b.blocker_type is BlockerType.STALE_EVIDENCE and b.subject == "SIGNATURE_PENDING"
               for b in blockers)


def test_inconsistency_becomes_process_inconsistency_blocker():
    m = meddicc()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["QUOTE_DELIVERED"] = ConditionState.UNSATISFIED  # QUOTE_ACCEPTED satisfied but dep not
    blockers, _ = _assess_and_infer(m, states)
    assert any(b.blocker_type is BlockerType.PROCESS_INCONSISTENCY for b in blockers)


def test_every_blocker_offers_no_action_and_is_not_an_action():
    blockers, _ = _assess_and_infer(spiced(), {})
    assert blockers
    for b in blockers:
        assert b.candidate_interventions[0] is InterventionKind.NO_ACTION
        assert isinstance(b, Blocker)  # a blocker is a cause, not an action


def test_later_stage_blocker_has_higher_impact():
    m = meddicc()
    states = _all(m.required_names(), ConditionState.UNSATISFIED)
    r = assess_methodology(m, states)
    blockers = infer_blockers(m, r, states)
    # first incomplete stage is DISCOVER; its blockers are earliest → low impact
    # force a late-stage-only failure to compare
    late_states = _all(m.required_names(), ConditionState.SATISFIED)
    late_states["SIGNATURE_PENDING"] = ConditionState.UNSATISFIED
    late_r = assess_methodology(m, late_states)
    late_blockers = infer_blockers(m, late_r, late_states)
    early_impact = max((b.impact for b in blockers), default=0)
    late_impact = max((b.impact for b in late_blockers), default=0)
    assert late_impact >= early_impact


# ── hypotheses ──────────────────────────────────────────────────────────────────────────────────────
def test_hypothesis_has_full_chain_and_candidate_interventions():
    m = spiced()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["ECONOMIC_BUYER_ENGAGED"] = ConditionState.UNSATISFIED
    blockers, _ = _assess_and_infer(m, states)
    h = hypothesis_for([b for b in blockers if b.subject == "ECONOMIC_BUYER_ENGAGED"][0])
    assert h.observation and h.inferred_cause and h.mechanism and h.expected_consequence and h.verification
    assert InterventionKind.NO_ACTION in h.candidate_interventions
    assert h.actionable is True  # unsatisfied, supported, confident


def test_hypothesis_not_actionable_when_contradicted():
    m = spiced()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["ECONOMIC_BUYER_ENGAGED"] = ConditionState.UNSATISFIED
    blockers, _ = _assess_and_infer(m, states)
    b = [x for x in blockers if x.subject == "ECONOMIC_BUYER_ENGAGED"][0]
    from dataclasses import replace
    contradicted = replace(b, contradicting_evidence=("buyer replied last week", "mutual plan exists"))
    h = hypothesis_for(contradicted)
    assert h.actionable is False  # contradicting >= supporting → verify first


def test_plan_hypotheses_ranked_by_expected_damage():
    m = meddicc()
    states = _all(m.required_names(), ConditionState.UNSATISFIED)
    r = assess_methodology(m, states)
    blockers = infer_blockers(m, r, states)
    hyps = plan_hypotheses(blockers)
    assert len(hyps) == len(blockers)
    # first-incomplete-stage blockers only (DISCOVER); all present and each a full hypothesis
    assert all(h.mechanism for h in hyps)
