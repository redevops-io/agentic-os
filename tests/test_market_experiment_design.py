"""Market & Funnel Intelligence Phase 6 — commercial hypothesis + frozen-evidence A/B experiment.

Proves: a hypothesis freezes into an experiment with a content-sealed success definition + evidence snapshot
(so results can't move the goalposts); and evaluation judges the outcome ONLY against that frozen definition —
guardrail breaches stop first, nothing is a win before the minimum observation window, and direction+threshold
decide improved/worse/no_effect.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import CommercialHypothesis, evaluate, freeze_experiment


def _hyp():
    return CommercialHypothesis(
        prov=Provenance(provider="market"), observed_pattern="3 competitors use daily-equivalent price framing",
        proposed_mechanism="lower-unit anchoring reduces perceived magnitude",
        affected_funnel_stage="pricing_page", expected_metric="signup_conversion", expected_direction="up",
        confidence=0.6, evidence_refs=("obs:1", "obs:2", "obs:3"))


def _exp(**kw):
    base = dict(control="monthly framing", treatment="daily-equivalent framing", target_surface="pricing_page",
                primary_metric="signup_conversion", success_threshold=0.01, minimum_observation_s=7 * 86400,
                guardrails={"refund_rate": 0.05})
    base.update(kw)
    return freeze_experiment(_hyp(), **base)


def test_freeze_seals_definition_and_evidence():
    e = _exp()
    assert e.evidence_seal and e.evidence_snapshot == ("obs:1", "obs:2", "obs:3")
    assert e.expected_direction == "up" and e.success_threshold == 0.01
    # the seal is a function of the frozen fields — same inputs → same seal (reproducible), proving it pins them
    assert _exp().evidence_seal == e.evidence_seal
    # a different success definition changes the seal (can't silently re-define success)
    assert _exp(success_threshold=0.02).evidence_seal != e.evidence_seal


def test_guardrail_breach_stops_first():
    e = _exp()
    ev = evaluate(e, primary_delta=0.03, elapsed_s=30 * 86400, guardrail_values={"refund_rate": -0.08})
    assert ev.verdict == "GUARDRAIL_STOP" and not ev.within_guardrails and "refund_rate" in ev.breached_guardrails


def test_inconclusive_before_minimum_observation():
    ev = evaluate(_exp(), primary_delta=0.05, elapsed_s=2 * 86400)   # big move but too early
    assert ev.verdict == "INCONCLUSIVE" and not ev.met_minimum_observation


def test_improved_when_direction_and_threshold_met():
    ev = evaluate(_exp(), primary_delta=0.02, elapsed_s=10 * 86400)
    assert ev.verdict == "IMPROVED" and ev.within_guardrails and ev.met_minimum_observation


def test_worse_when_moves_against_hypothesis():
    ev = evaluate(_exp(), primary_delta=-0.02, elapsed_s=10 * 86400)
    assert ev.verdict == "WORSE"


def test_no_effect_when_below_threshold():
    ev = evaluate(_exp(), primary_delta=0.003, elapsed_s=10 * 86400)   # positive but under 0.01 threshold
    assert ev.verdict == "NO_EFFECT"


def test_down_direction_hypothesis():
    # a hypothesis expecting a metric to go DOWN (e.g. cart abandonment)
    e = _exp(primary_metric="cart_abandonment")
    e = freeze_experiment(
        CommercialHypothesis(prov=Provenance(provider="m"), expected_metric="cart_abandonment",
                             expected_direction="down", evidence_refs=("o1",)),
        control="c", treatment="t", target_surface="checkout", primary_metric="cart_abandonment",
        success_threshold=0.02, minimum_observation_s=0)
    assert evaluate(e, primary_delta=-0.03, elapsed_s=1).verdict == "IMPROVED"   # down 0.03 ≥ 0.02 → win
    assert evaluate(e, primary_delta=0.03, elapsed_s=1).verdict == "WORSE"       # went up → worse
