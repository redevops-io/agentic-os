"""Automotive domain contracts + vPIC VIN decode (P0 of the WhatsApp car-diagnosis service)."""
from __future__ import annotations

import os

import pytest

from agentic_os.automotive import (
    Diagnosis,
    DiagnosticEvidenceRequest,
    DiagnosticObservation,
    EvidenceType,
    Hypothesis,
    Powertrain,
    Severity,
    SymptomEvidence,
    Urgency,
    VehicleRef,
    decode_vin,
)


def test_vehicle_ref_label_and_hash_stable():
    v = VehicleRef(vin="1hgcm82633a004352", make="Honda", model="Accord", year="2003",
                   powertrain=Powertrain.ICE)
    assert v.label == "2003 Honda Accord"
    assert v.canonical_form()["vin"] == "1HGCM82633A004352"   # normalized upper


def test_observation_and_symptom_are_content_addressed():
    o = DiagnosticObservation(kind="dtc", code="P0302", source="obd", observed_at="2026-09-06T00:00:00Z")
    assert o.evidence_id.startswith("rcv1:")
    s = SymptomEvidence(component="engine", narrative="shakes at idle", media_refs=("m2", "m1"))
    # media_refs order-independent
    assert s.evidence_id == SymptomEvidence(component="engine", narrative="shakes at idle",
                                            media_refs=("m1", "m2")).evidence_id


def test_fractional_fields_are_decimal_strings_not_floats():
    h = Hypothesis(cause="cyl 2 misfire", confidence=0.78, cost_low=120, cost_high=340)
    assert h.confidence == "0.78" and h.cost_low == "120" and h.cost_high == "340"
    req = DiagnosticEvidenceRequest(type=EvidenceType.OBD_SNAPSHOT, expected_information_gain=0.4,
                                    estimated_cost=0)
    assert req.expected_information_gain == "0.4"


def test_evidence_request_value_per_burden_ranks_cheap_high_gain_first():
    cheap_q = DiagnosticEvidenceRequest(type=EvidenceType.QUESTION, expected_information_gain="0.5",
                                        estimated_cost="0", user_effort="low")
    pricey_scan = DiagnosticEvidenceRequest(type=EvidenceType.OBD_SNAPSHOT,
                                            expected_information_gain="0.6", estimated_cost="30",
                                            user_effort="high")
    assert cheap_q.value_per_burden > pricey_scan.value_per_burden   # ask the free question first


def test_hypothesis_safety_critical_detection():
    brakes = Hypothesis(cause="worn pads", system="brakes")
    critical = Hypothesis(cause="x", severity=Severity.CRITICAL)
    benign = Hypothesis(cause="cabin filter", system="hvac", severity=Severity.LOW)
    assert brakes.is_safety_critical and critical.is_safety_critical and not benign.is_safety_critical


def test_diagnosis_top_and_id():
    d = Diagnosis(case_id="c1",
                  hypotheses=(Hypothesis(cause="misfire", confidence="0.78"),
                              Hypothesis(cause="injector", confidence="0.14")))
    assert d.top.cause == "misfire"
    assert d.diagnosis_id.startswith("rcv1:")


# ---- vPIC VIN decode (offline mocked) ----

_HONDA = {"Results": [{"Make": "HONDA", "Model": "Accord", "ModelYear": "2003",
                       "FuelTypePrimary": "Gasoline", "BodyClass": "Coupe", "EngineCylinders": "6"}]}
_TESLA = {"Results": [{"Make": "TESLA", "Model": "Model 3", "ModelYear": "2022",
                       "FuelTypePrimary": "Electric", "ElectrificationLevel": "BEV"}]}
_PRIUS = {"Results": [{"Make": "TOYOTA", "Model": "Prius", "ModelYear": "2018",
                       "FuelTypePrimary": "Gasoline", "FuelTypeSecondary": "Electric",
                       "ElectrificationLevel": "Strong HEV"}]}


def test_decode_vin_maps_fields_and_infers_powertrain():
    v = decode_vin("1HGCM82633A004352", fetch=lambda url: _HONDA)
    assert (v.make, v.model, v.year) == ("Honda", "Accord", "2003")
    assert v.powertrain is Powertrain.ICE and v.engine_cylinders == "6"
    assert decode_vin("x", fetch=lambda url: _TESLA).powertrain is Powertrain.EV
    assert decode_vin("x", fetch=lambda url: _PRIUS).powertrain is Powertrain.HYBRID


def test_decode_vin_degrades_gracefully_on_error():
    def boom(url): raise OSError("no network")
    v = decode_vin("1HGCM82633A004352", fetch=boom)
    assert v.vin == "1HGCM82633A004352" and v.make == ""   # bare ref, diagnosis can still proceed


@pytest.mark.skipif(os.environ.get("NO_NET") == "1", reason="live vPIC smoke")
def test_decode_vin_live():
    v = decode_vin("1HGCM82633A004352")   # real vPIC
    assert v.make == "Honda" and v.model == "Accord" and v.powertrain is Powertrain.ICE
