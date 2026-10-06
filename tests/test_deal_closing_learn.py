"""Deal Closing Intelligence Phase 7 — attribution ladder + scoped outcome learning.

Proves: an unexecuted intervention attributes to NONE; the rung is the highest observed signal; a won deal with
many competing interventions attributes WEAKLY (the ladder separates 'deal closed' from 'this closed it'); priors
are learned per scoped context (intervention x blocker x segment/size/stage) and shrunk toward neutral; and priors
reweight an action's P(resolves) without gating NO_ACTION or an un-learned kind.
"""
from __future__ import annotations

from agentic_os.deal_closing import (
    AttributionRung, BlockerType, ClosingInterventionOutcome, InterventionKind, InterventionLedger, apply_priors,
    attribution_strength, calibrate, classify_rung,
)
from agentic_os.deal_closing.candidates import CandidateAction, no_action
from agentic_os.deal_closing.contracts import Provenance

_DAY = 86_400.0


def _o(**kw):
    base = dict(prov=Provenance(provider="deal_closing"), deal_ref="d1",
                intervention_kind=InterventionKind.ENGAGE_PROCUREMENT, blocker_type=BlockerType.PROCUREMENT_BLOCKER,
                target_condition="PROCUREMENT_ENGAGED")
    base.update(kw)
    return ClosingInterventionOutcome(**base)


# ── attribution ladder ──────────────────────────────────────────────────────────────────────────────
def test_unexecuted_is_none():
    assert classify_rung(_o(executed=False, condition_changed=True)) is AttributionRung.NONE
    assert attribution_strength(_o(executed=False)) == 0.0


def test_rung_is_highest_observed_signal():
    assert classify_rung(_o(executed=True)) is AttributionRung.EXECUTED
    assert classify_rung(_o(executed=True, condition_changed=True)) is AttributionRung.BLOCKER_CHANGED
    # missing intermediate (no immediate_response) does not erase a later observed effect
    assert classify_rung(_o(executed=True, stage_progressed=True)) is AttributionRung.STAGE_PROGRESSED
    assert classify_rung(_o(executed=True, won=True)) is AttributionRung.OUTCOME_REACHED
    assert classify_rung(_o(executed=True, controlled=True)) is AttributionRung.CONTROLLED


def test_won_with_many_competitors_attributes_weakly():
    clean = _o(executed=True, condition_changed=True, stage_progressed=True, won=True, elapsed_s=1 * _DAY,
               competing_interventions=0)
    noisy = _o(executed=True, condition_changed=True, stage_progressed=True, won=True, elapsed_s=1 * _DAY,
               competing_interventions=8)
    assert classify_rung(clean) is classify_rung(noisy)       # same rung (same signals)
    assert attribution_strength(noisy) < attribution_strength(clean)   # but weaker attribution


def test_controlled_is_strongest():
    controlled = _o(executed=True, condition_changed=True, won=True, controlled=True, elapsed_s=1 * _DAY)
    uncontrolled = _o(executed=True, condition_changed=True, won=True, elapsed_s=1 * _DAY)
    assert attribution_strength(controlled) > attribution_strength(uncontrolled)


# ── scoped learning ─────────────────────────────────────────────────────────────────────────────────
def test_priors_are_scoped_and_shrunk():
    outcomes = [
        _o(executed=True, condition_changed=True, segment="mid_market", size_band="M", stage="QUALIFY"),
        _o(executed=True, condition_changed=True, segment="mid_market", size_band="M", stage="QUALIFY"),
        _o(executed=True, condition_changed=False, segment="enterprise", size_band="XL", stage="QUALIFY"),
        _o(executed=False),                                    # carries no signal
    ]
    priors = calibrate(outcomes, strength=4)
    # procurement engagement worked in mid_market context → above neutral but shrunk below 1.0
    p_mm = priors.p_resolves(intervention_kind=InterventionKind.ENGAGE_PROCUREMENT,
                             blocker_type=BlockerType.PROCUREMENT_BLOCKER, segment="mid_market",
                             size_band="M", stage="QUALIFY")
    assert 0.5 < p_mm < 1.0
    # an unseen context falls back to a broader scope / neutral, never errors
    p_unseen = priors.p_resolves(intervention_kind=InterventionKind.SEND_QUOTE)
    assert p_unseen == 0.5


def test_apply_priors_reweights_without_gating():
    outcomes = [_o(executed=True, condition_changed=True) for _ in range(4)]
    priors = calibrate(outcomes, strength=2)
    actions = (
        no_action("d1", "PROCUREMENT_ENGAGED"),
        CandidateAction(kind=InterventionKind.ENGAGE_PROCUREMENT, deal_ref="d1",
                        target_condition="PROCUREMENT_ENGAGED", p_resolves=0.4),
    )
    reweighted = apply_priors(actions, priors, blocker_type=BlockerType.PROCUREMENT_BLOCKER)
    na = [a for a in reweighted if a.kind is InterventionKind.NO_ACTION][0]
    ep = [a for a in reweighted if a.kind is InterventionKind.ENGAGE_PROCUREMENT][0]
    assert na.p_resolves == 0.0                       # NO_ACTION untouched
    assert ep.p_resolves > 0.4                         # lifted by the positive learned prior
    assert ep.p_resolves <= 1.0


def test_ledger_accumulates_and_calibrates():
    ledger = InterventionLedger()
    for _ in range(3):
        ledger.record(_o(executed=True, condition_changed=True))
    assert len(ledger.outcomes()) == 3
    priors = ledger.calibrate(strength=2)
    assert priors.p_resolves(intervention_kind=InterventionKind.ENGAGE_PROCUREMENT,
                             blocker_type=BlockerType.PROCUREMENT_BLOCKER) > 0.5
