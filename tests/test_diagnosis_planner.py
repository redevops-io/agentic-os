"""Evidence planner + safety gate + Diagnoser (P1) — deterministic core of the diagnostic Mission."""
from __future__ import annotations

import json

from agentic_os.automotive import (
    Diagnoser,
    DiagnosticEvidenceRequest,
    DiagnosticObservation,
    EvidenceType,
    Hypothesis,
    Severity,
    SymptomEvidence,
    Urgency,
    VehicleRef,
    format_reply,
    is_confident,
    plan_next_evidence,
    safety_gate,
    should_stop_collecting,
)


# ---- safety gate ----

def test_safety_gate_blocks_when_a_critical_cause_is_plausible():
    hyps = [Hypothesis(cause="worn cabin filter", confidence="0.6", system="hvac", severity=Severity.LOW),
            Hypothesis(cause="brake caliper seizing", confidence="0.2", system="brakes")]
    a = safety_gate(hyps)
    assert a.passed is False and "brake" in a.advice.lower()


def test_safety_gate_passes_when_no_critical_cause():
    hyps = [Hypothesis(cause="cabin filter", confidence="0.9", system="hvac", severity=Severity.LOW)]
    assert safety_gate(hyps).passed is True


def test_safety_gate_stop_driving_on_high_confidence_critical():
    hyps = [Hypothesis(cause="brake failure", confidence="0.9", system="brakes",
                       urgency=Urgency.STOP_DRIVING, severity=Severity.CRITICAL)]
    a = safety_gate(hyps)
    assert a.passed is False and a.stop_driving is True


# ---- planner ----

def test_plan_next_prefers_cheap_high_gain_and_skips_already_have():
    cands = [DiagnosticEvidenceRequest(type=EvidenceType.QUESTION, prompt="steady or flashing light?",
                                       expected_information_gain="0.5", estimated_cost="0", user_effort="low"),
             DiagnosticEvidenceRequest(type=EvidenceType.OBD_SNAPSHOT, expected_information_gain="0.6",
                                       estimated_cost="30", user_effort="high"),
             DiagnosticEvidenceRequest(type=EvidenceType.DTC, expected_information_gain="0.9",
                                       estimated_cost="0")]
    # DTC already collected → excluded; cheap question beats the pricey scan
    nxt = plan_next_evidence(cands, already_have=[EvidenceType.DTC])
    assert nxt.type is EvidenceType.QUESTION


def test_plan_next_safety_first():
    cands = [DiagnosticEvidenceRequest(type=EvidenceType.QUESTION, expected_information_gain="0.9",
                                       estimated_cost="0", safety_risk="none"),
             DiagnosticEvidenceRequest(type=EvidenceType.PHOTO, prompt="photo of the brake fluid",
                                       expected_information_gain="0.4", estimated_cost="0",
                                       safety_risk="brakes")]
    assert plan_next_evidence(cands, safety_first=True).type is EvidenceType.PHOTO


def test_should_stop_when_confident():
    hyps = [Hypothesis(cause="misfire", confidence="0.85")]
    stop, nxt = should_stop_collecting(hyps, [], already_have=[])
    assert stop is True and nxt is None
    assert is_confident(hyps) is True


# ---- Diagnoser (fake LLM) ----

def _fake_llm(response_json):
    return lambda system, user: json.dumps(response_json)


def test_diagnoser_ranks_and_gates():
    llm = _fake_llm({"hypotheses": [
        {"cause": "cylinder 2 ignition misfire", "confidence": 0.78, "system": "engine",
         "severity": "high", "urgency": "urgent", "cost_low": 120, "cost_high": 340,
         "recommended_action": "inspect coil/plug on cyl 2"},
        {"cause": "fuel injector", "confidence": 0.14, "system": "engine"}],
        "evidence_requests": [{"type": "question", "prompt": "is the light flashing?",
                               "expected_information_gain": 0.5, "estimated_cost": 0}]})
    d = Diagnoser(llm=llm)
    diag = d.diagnose(case_id="c1", vehicle=VehicleRef(make="Honda", model="Accord", year="2019"),
                      observations=[DiagnosticObservation(kind="dtc", code="P0302")],
                      symptoms=[SymptomEvidence(narrative="shakes at idle")])
    assert diag.top.cause == "cylinder 2 ignition misfire"
    assert diag.safety_gate_passed is True            # engine misfire (not a safety-critical system)
    assert diag.diagnosis_id.startswith("rcv1:")


def test_diagnoser_safety_gate_trips_on_brakes():
    llm = _fake_llm({"hypotheses": [
        {"cause": "brake caliper seizing", "confidence": 0.55, "system": "brakes",
         "severity": "high", "urgency": "urgent"}]})
    diag = Diagnoser(llm=llm).diagnose(case_id="c1", vehicle=VehicleRef(make="Ford", model="F150"))
    assert diag.safety_gate_passed is False
    reply = format_reply(diag)
    assert "⚠️" in reply and "brake" in reply.lower()


def test_diagnoser_handles_markdown_fenced_json():
    llm = lambda s, u: "```json\n" + json.dumps({"hypotheses": [
        {"cause": "dead 12V battery", "confidence": 0.8, "system": "electrical"}]}) + "\n```"
    diag = Diagnoser(llm=llm).diagnose(case_id="c1", vehicle=VehicleRef(make="Toyota"))
    assert diag.top.cause == "dead 12V battery"


def test_format_reply_is_decision_support():
    llm = _fake_llm({"hypotheses": [
        {"cause": "cyl 2 misfire", "confidence": 0.78, "system": "engine", "urgency": "urgent",
         "cost_low": 120, "cost_high": 340, "recommended_action": "inspect coil"}]})
    diag = Diagnoser(llm=llm).diagnose(case_id="c1", vehicle=VehicleRef(make="Honda"))
    r = format_reply(diag)
    assert "What's most likely" in r and "cyl 2 misfire" in r and "78%" in r
    assert "How urgent" in r and "$120" in r


def test_diagnoser_asks_for_more_when_unsure():
    llm = _fake_llm({"hypotheses": [
        {"cause": "maybe injector", "confidence": 0.3, "system": "engine"}],
        "evidence_requests": [{"type": "obd_snapshot", "prompt": "connect a scanner",
                               "expected_information_gain": 0.7, "estimated_cost": 0}]})
    diag = Diagnoser(llm=llm).diagnose(case_id="c1", vehicle=VehicleRef(make="Kia"))
    assert diag.needs_more_evidence is True and diag.next_request.type is EvidenceType.OBD_SNAPSHOT
    assert "connect a scanner" in format_reply(diag)
