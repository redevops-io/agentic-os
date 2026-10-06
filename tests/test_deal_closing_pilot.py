"""Deal Closing Intelligence Phase 5 — shadow pilot / recommendation scorecard.

Proves: the engine's primary blocker is scored against a human label (agreement); flagged blockers the human says
aren't real count as false positives; recommended actions are scored for acceptance; the engine catches pipeline
inflation (CRM stage ahead of the evidence-derived stage); and every metric ABSTAINS when its label is absent so
an unlabelled deal lowers no score. The pilot runs at RECOMMEND — no side effects.
"""
from __future__ import annotations

from agentic_os.deal_closing import (
    AutonomyLevel, ConditionState, InterventionKind, LabeledDeal, evaluate_deal, render_scorecard, run_pilot,
    spiced,
)
from agentic_os.deal_closing.contracts import Deal, Provenance


def _deal(stage="DISCOVER", ref="sf:1"):
    return Deal(prov=Provenance(provider="salesforce"), opportunity_ref=ref, reported_stage=stage,
                reported_amount_cents=500_000_00, currency="USD")


def _states(m, **overrides):
    st = {n: ConditionState.SATISFIED for n in m.required_names()}
    st.update(overrides)
    return st


def test_catches_pipeline_inflation():
    m = spiced()
    # CRM says PROPOSE, but DISCOVER conditions aren't even verified → inflated
    states = {n: ConditionState.UNKNOWN for n in m.required_names()}
    labeled = LabeledDeal(deal=_deal(stage="PROPOSE"), methodology=m, states=states)
    ev = evaluate_deal(labeled)
    assert ev.reported_stage_idx == m.stages.index("PROPOSE")
    assert ev.derived_stage_idx < ev.reported_stage_idx
    assert ev.inflation is True


def test_blocker_agreement_and_false_positives():
    m = spiced()
    states = _states(m, ECONOMIC_BUYER_ENGAGED=ConditionState.UNSATISFIED)
    labeled = LabeledDeal(deal=_deal(stage="PROPOSE"), methodology=m, states=states,
                          primary_blocker="ECONOMIC_BUYER_ENGAGED",
                          real_blockers=("ECONOMIC_BUYER_ENGAGED",))
    ev = evaluate_deal(labeled)
    assert ev.engine_primary_blocker == "ECONOMIC_BUYER_ENGAGED"
    assert ev.blocker_agreement is True
    assert ev.false_positives == ()  # the only flagged blocker is the real one


def test_false_positive_detected_when_human_disagrees():
    m = spiced()
    states = _states(m, ECONOMIC_BUYER_ENGAGED=ConditionState.UNSATISFIED)
    # human says nothing is really blocking → the engine's flag is a false positive
    labeled = LabeledDeal(deal=_deal(stage="PROPOSE"), methodology=m, states=states, real_blockers=())
    ev = evaluate_deal(labeled)
    assert "ECONOMIC_BUYER_ENGAGED" in ev.false_positives


def test_acceptance_scored_against_label():
    m = spiced()
    states = _states(m, ECONOMIC_BUYER_ENGAGED=ConditionState.UNSATISFIED)
    labeled = LabeledDeal(deal=_deal(stage="PROPOSE"), methodology=m, states=states,
                          accepted_actions=(InterventionKind.ENGAGE_ECONOMIC_BUYER,
                                            InterventionKind.STRENGTHEN_CHAMPION, InterventionKind.VERIFY_CLAIM))
    ev = evaluate_deal(labeled)
    assert ev.accepted_hits is not None
    hits, total = ev.accepted_hits
    assert total >= 1 and hits >= 1


def test_metrics_abstain_without_labels():
    m = spiced()
    states = _states(m, ECONOMIC_BUYER_ENGAGED=ConditionState.UNSATISFIED)
    card = run_pilot((LabeledDeal(deal=_deal(stage="PROPOSE"), methodology=m, states=states),))
    assert card.blocker_agreement.rate is None     # no primary_blocker label
    assert card.false_positive.rate is None         # no real_blockers label
    assert card.acceptance.rate is None             # no accepted_actions label
    assert card.inflation_caught.rate is not None   # inflation needs no label (uses reported stage)


def test_scorecard_aggregates_and_renders():
    m = spiced()
    deals = (
        LabeledDeal(deal=_deal(stage="PROPOSE", ref="sf:1"), methodology=m,
                    states=_states(m, ECONOMIC_BUYER_ENGAGED=ConditionState.UNSATISFIED),
                    primary_blocker="ECONOMIC_BUYER_ENGAGED", real_blockers=("ECONOMIC_BUYER_ENGAGED",),
                    accepted_actions=(InterventionKind.ENGAGE_ECONOMIC_BUYER,)),
        LabeledDeal(deal=_deal(stage="CLOSE", ref="sf:2"), methodology=m, states=_states(m),
                    primary_blocker="", real_blockers=("",)),  # ready to close
    )
    card = run_pilot(deals)
    assert card.n == 2
    assert card.ready_to_close == 1
    assert card.blocker_agreement.den == 1   # only one deal had a primary_blocker label
    md = render_scorecard(card)
    assert "shadow pilot scorecard (2 deals)" in md
    assert "Pipeline inflation caught" in md


def test_pilot_runs_at_recommend_no_execute():
    m = spiced()
    card = run_pilot((LabeledDeal(deal=_deal(stage="PROPOSE"), methodology=m,
                                  states=_states(m, ECONOMIC_BUYER_ENGAGED=ConditionState.UNSATISFIED)),),
                     autonomy=AutonomyLevel.RECOMMEND)
    assert card.n == 1
