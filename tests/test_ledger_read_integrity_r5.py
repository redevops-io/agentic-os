"""SR-04 round 5 (LG-2): attested metrics are verified on READ as well as on record. A metric mutated after it was
recorded is never served. Exact copies of a valid metric keep a valid attestation (by design); a copy that differs
in content does not."""
from __future__ import annotations

import dataclasses

import pytest

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market import FirstPartyMetric
from agentic_os.market.experiment_design import CommercialHypothesis, freeze_experiment
from agentic_os.market.ingestion import IngestionRefused, repo_adapter
from agentic_os.market.outcomes_join import FirstPartyLedger, conversion_delta, measure_experiment


def mint(metric="signup", value=0.5, segment="treatment", **kw):
    return repo_adapter("umami").make_metric(metric=metric, value=value, segment=segment, **kw)


def tampered_ledger():
    led = FirstPartyLedger()
    m = mint("conversion", 0.1, "control")
    led.record(m)
    led.record(mint("conversion", 0.2, "treatment"))
    object.__setattr__(m, "value", 123.0)                                  # mutation AFTER recording
    return led


def test_a_mutated_recorded_metric_is_never_served_by_any_reader():
    led = tampered_ledger()
    for read in (lambda: led.value("conversion", "control"), lambda: led.get("conversion", "control"),
                 lambda: led.metrics(), lambda: list(led),
                 lambda: led.measured_metric_names(), lambda: conversion_delta(led, "conversion",
                                                                               baseline_segment="control",
                                                                               recent_segment="treatment")):
        with pytest.raises(IngestionRefused, match="attestation|integrity"):
            read()


def test_higher_is_better_verifies_the_metric_it_reads():
    led = FirstPartyLedger()
    m = mint("conversion", 0.1, "treatment")
    led.record(m)
    object.__setattr__(m, "higher_is_better", False)
    with pytest.raises(IngestionRefused):
        led.higher_is_better("conversion")


def test_untampered_reads_work_and_are_ordered():
    led = FirstPartyLedger()
    led.record(mint("b", 0.2, "treatment"))
    led.record(mint("a", 0.1, "control"))
    assert led.value("a", "control") == 0.1 and led.get("zzz", "control") is None and led.value("zzz", "x") is None
    assert [(m.metric, m.segment) for m in led] == [("a", "control"), ("b", "treatment")] and len(led) == 2
    assert led.measured_metric_names() == ("a", "b") and led.higher_is_better("a") is True


def test_every_kind_of_post_record_mutation_is_detected():
    for field, new in (("value", 9.0), ("metric", "other"), ("segment", "x"), ("surface", "s"), ("source", "gsc"),
                       ("higher_is_better", False), ("period_start_ms", 5), ("attestation", "")):
        led = FirstPartyLedger()
        m = mint("m", 0.1, "control")
        led.record(m)
        object.__setattr__(m, field, new)
        with pytest.raises((IngestionRefused, KeyError, ValueError)):
            led.metrics()
    led = FirstPartyLedger()
    m = mint("m", 0.1, "control")
    led.record(m)
    object.__setattr__(m.prov, "provider_ref", "forged")
    with pytest.raises(IngestionRefused):
        led.value("m", "control")


def test_an_entry_written_around_record_is_refused_on_read_and_the_storage_is_private():
    led = FirstPartyLedger()
    assert not hasattr(led, "_by_key")                                      # no public / conventional handle
    bad = FirstPartyMetric(prov=Provenance(provider="umami"), source="umami", metric="y", value=5.0, segment="control")
    led._FirstPartyLedger__entries[("y", "control")] = bad                    # deliberate private poke (documented)
    with pytest.raises(IngestionRefused):
        led.value("y", "control")
    led2 = FirstPartyLedger()
    led2._FirstPartyLedger__entries[("y", "control")] = {"source": "umami"}      # type: ignore[assignment]
    with pytest.raises(IngestionRefused):
        led2.get("y", "control")


def test_a_refused_record_leaves_the_ledger_unchanged():
    led = FirstPartyLedger()
    led.record(mint("a", 0.1, "control"))
    with pytest.raises(ValueError):
        led.record(FirstPartyMetric(prov=Provenance(provider="umami"), source="umami", metric="a", value=7.0,
                                    segment="control"))
    assert led.value("a", "control") == 0.1 and len(led) == 1


def test_exact_copies_keep_a_valid_attestation_by_design_but_content_changes_do_not():
    m = mint()
    led = FirstPartyLedger()
    assert led.record(dataclasses.replace(m)).value == 0.5                   # same content: valid (by design)
    with pytest.raises(IngestionRefused):
        led.record(dataclasses.replace(m, value=0.6))                         # different content: refused


def test_measure_experiment_fails_closed_on_a_tampered_ledger():
    h = CommercialHypothesis(prov=Provenance(provider="m"), expected_metric="signup_conversion",
                             expected_direction="up", evidence_refs=("o1",))
    exp = freeze_experiment(h, control="a", treatment="b", target_surface="p", primary_metric="signup_conversion",
                            success_threshold=0.01, minimum_observation_s=86400)
    led = FirstPartyLedger()
    c = mint("signup_conversion", 0.10, "control")
    led.record(c)
    led.record(mint("signup_conversion", 0.12, "treatment"))
    assert measure_experiment(exp, led, elapsed_s=2 * 86400).verdict
    object.__setattr__(c, "value", 0.0)
    with pytest.raises(IngestionRefused):
        measure_experiment(exp, led, elapsed_s=2 * 86400)


def test_the_pr_claim_about_copies_is_corrected_in_the_docs():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "ledger_source_inventory.md").read_text().lower()
    assert "exact copy" in text and "every read" in text
