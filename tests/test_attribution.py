"""Tests for multi-factor outcome attribution (agentic_os.attribution)."""
from __future__ import annotations

import pytest

from agentic_os.attribution import (
    Attribution, AttributionFactors, attribution_confidence, correlate, derive_outcome,
    temporal_proximity)
from agentic_os.intervention_record import InterventionRecord
from agentic_os.observation import Observation

HOUR = 3600.0


def _rec(iid, subject_ref, executed_at, execution_ref=""):
    return InterventionRecord(
        intervention_id=iid, opportunity_id=f"crm:{subject_ref}", candidate_id=f"crm:{subject_ref}:send",
        selected_action="send_proposal", alternatives=(), evidence_refs=(), policy_version="p1",
        score=0.5, proposed_at=executed_at, executed_at=executed_at, execution_ref=execution_ref)


def _obs(subject, valid_at, payload=None, refs=()):
    return Observation(observation_id="oc-1", source="crm", kind="crm.reply", subject=subject,
                       valid_at=valid_at, known_at=valid_at, payload=payload or {}, evidence_refs=refs)


# ── the confidence function ───────────────────────────────────────────────────────────
def test_delay_alone_does_not_decide_attribution():
    quick_but_crowded = AttributionFactors(temporal_proximity=1.0, causal_link_strength=0.5,
                                           competing_interventions=1.0, explicit_correlation=0.0,
                                           outcome_specificity=0.4, observation_quality=0.7)
    slow_but_clean = AttributionFactors(temporal_proximity=0.3, causal_link_strength=0.9,
                                        competing_interventions=0.0, explicit_correlation=1.0,
                                        outcome_specificity=0.9, observation_quality=0.9)
    # the slower-but-clean, explicitly-correlated, uncontested outcome is MORE attributable
    assert attribution_confidence(slow_but_clean) > attribution_confidence(quick_but_crowded)


def test_competing_interventions_discount_confidence():
    base = AttributionFactors(temporal_proximity=0.9, causal_link_strength=0.8, explicit_correlation=0.8,
                              outcome_specificity=0.8, observation_quality=0.9, competing_interventions=0.0)
    crowded = AttributionFactors(**{**base.__dict__, "competing_interventions": 1.0})
    assert attribution_confidence(crowded) < attribution_confidence(base)


def test_temporal_proximity_half_life():
    assert temporal_proximity(0.0, HOUR) == pytest.approx(1.0)
    assert temporal_proximity(HOUR, HOUR) == pytest.approx(0.5, abs=1e-6)
    assert temporal_proximity(3 * HOUR, HOUR) < 0.2


# ── correlation ───────────────────────────────────────────────────────────────────────
def test_correlate_picks_the_nearest_prior_intervention_and_counts_competitors():
    interventions = [_rec("iv-old", "Acme", executed_at=0.0), _rec("iv-near", "Acme", executed_at=90 * 60)]
    obs = _obs("Acme", valid_at=91 * 60)                    # 1 min after iv-near
    att = correlate(obs, interventions, half_life_seconds=2 * HOUR)
    assert att is not None and att.intervention.intervention_id == "iv-near"
    assert att.factors.competing_interventions > 0          # iv-old competes
    assert 0.0 < att.confidence <= 1.0


def test_correlate_returns_none_when_nothing_plausible():
    # the only intervention is AFTER the observation (can't have caused it) → no attribution
    interventions = [_rec("iv1", "Acme", executed_at=1000.0)]
    assert correlate(_obs("Acme", valid_at=500.0), interventions, half_life_seconds=HOUR) is None
    # and an intervention outside the attribution window is ignored
    interventions = [_rec("iv1", "Acme", executed_at=0.0)]
    assert correlate(_obs("Acme", valid_at=1e9), interventions, half_life_seconds=HOUR,
                     window_seconds=HOUR) is None


def test_explicit_correlation_lifts_confidence():
    interventions = [_rec("iv1", "Acme", executed_at=0.0, execution_ref="msg-42")]
    plain = correlate(_obs("Acme", 600.0), interventions, half_life_seconds=HOUR)
    explicit = correlate(_obs("Acme", 600.0, payload={"in_reply_to": "msg-42"}), interventions,
                         half_life_seconds=HOUR)
    assert explicit.confidence > plain.confidence
    assert explicit.factors.explicit_correlation == 1.0


# ── derive the OutcomeEvent ─────────────────────────────────────────────────────────
def test_derive_outcome_builds_event_and_link():
    interventions = [_rec("iv1", "Acme", executed_at=0.0)]
    att = correlate(_obs("Acme", valid_at=2 * HOUR), interventions, half_life_seconds=HOUR)
    ev, link = derive_outcome(_obs("Acme", valid_at=2 * HOUR), att, reward_dimensions={"reply": 1.0})
    assert ev.action_kind == "send_proposal" and ev.reward_dimensions == {"reply": 1.0}
    assert ev.delay == pytest.approx(2 * HOUR) and ev.attribution_confidence == att.confidence
    assert link.intervention_id == "iv1" and link.outcome_ref == "oc-1"
    # scalar reward is attribution-weighted
    assert ev.scalar_reward() == pytest.approx(1.0 * att.confidence)
