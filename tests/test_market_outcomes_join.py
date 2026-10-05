"""Market & Funnel Intelligence Phase 7 — first-party outcome join (OUR metrics decide).

Proves: an experiment is measured from a first-party metric ledger (control vs treatment) and evaluated via the
Phase-6 frozen definition; a missing primary reading is inconclusive (never guessed); guardrail polarity is
honoured via higher_is_better so an adverse move trips the guardrail regardless of metric direction.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import (
    CommercialHypothesis, FirstPartyLedger, FirstPartyMetric, conversion_delta, freeze_experiment,
    measure_experiment,
)


def _exp():
    h = CommercialHypothesis(prov=Provenance(provider="m"), expected_metric="signup_conversion",
                             expected_direction="up", evidence_refs=("o1",))
    return freeze_experiment(h, control="monthly", treatment="daily-equivalent", target_surface="pricing_page",
                             primary_metric="signup_conversion", success_threshold=0.01,
                             minimum_observation_s=7 * 86400, guardrails={"refund_rate": 0.03})


def _m(metric, value, segment, higher=True):
    return FirstPartyMetric(prov=Provenance(provider="umami"), source="umami", metric=metric, value=value,
                            segment=segment, higher_is_better=higher)


def test_measures_improvement_from_ledger():
    led = FirstPartyLedger()
    led.record(_m("signup_conversion", 0.10, "control"))
    led.record(_m("signup_conversion", 0.12, "treatment"))     # +0.02 ≥ 0.01 threshold
    led.record(_m("refund_rate", 0.02, "control", higher=False))
    led.record(_m("refund_rate", 0.02, "treatment", higher=False))
    out = measure_experiment(_exp(), led, elapsed_s=10 * 86400)
    assert out.verdict == "IMPROVED" and abs(out.primary_delta - 0.02) < 1e-9
    assert out.control_value == 0.10 and out.treatment_value == 0.12


def test_missing_primary_reading_is_inconclusive_not_guessed():
    led = FirstPartyLedger()
    led.record(_m("signup_conversion", 0.10, "control"))       # no treatment reading yet
    out = measure_experiment(_exp(), led, elapsed_s=10 * 86400)
    assert out.verdict == "INCONCLUSIVE" and out.treatment_value is None and out.primary_delta == 0.0


def test_guardrail_polarity_trips_on_adverse_move():
    led = FirstPartyLedger()
    led.record(_m("signup_conversion", 0.10, "control"))
    led.record(_m("signup_conversion", 0.13, "treatment"))     # primary improved...
    led.record(_m("refund_rate", 0.02, "control", higher=False))
    led.record(_m("refund_rate", 0.07, "treatment", higher=False))  # ...but refunds rose +0.05 (>0.03 limit)
    out = measure_experiment(_exp(), led, elapsed_s=10 * 86400)
    assert out.verdict == "GUARDRAIL_STOP" and "refund_rate" in out.evaluation.breached_guardrails
    assert out.guardrail_values["refund_rate"] < 0           # signed goodness: a refund rise is adverse


def test_conversion_delta_helper():
    led = FirstPartyLedger()
    led.record(_m("conversion", 0.20, "control"))
    led.record(_m("conversion", 0.18, "treatment"))
    assert conversion_delta(led) == -0.1                      # down 10%
    assert conversion_delta(FirstPartyLedger()) is None       # no readings → None, not 0
