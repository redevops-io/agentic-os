"""P4 — shop handoff packet + repair-outcome loop (the flywheel)."""
from __future__ import annotations

import json

from runtime_contracts import Channel, InteractionEvent, Modality
from agentic_os.automotive import (
    Diagnoser,
    Diagnosis,
    DiagnosisService,
    DiagnosticObservation,
    Hypothesis,
    OutcomeExtractor,
    Severity,
    SymptomEvidence,
    Urgency,
    VehicleRef,
    build_handoff,
    format_handoff,
    format_outcome_ack,
    looks_like_handoff,
    looks_like_outcome,
)


def test_handoff_hint_and_outcome_hint():
    assert looks_like_handoff("send to shop") and looks_like_handoff("/handoff")
    assert not looks_like_handoff("my engine is knocking")
    assert looks_like_outcome("they replaced the coil and it's fixed now")
    assert looks_like_outcome("still happening, not fixed")
    assert not looks_like_outcome("what's wrong with my car")


def test_build_and_format_handoff():
    diag = Diagnosis(case_id="c1", hypotheses=(
        Hypothesis(cause="cyl2 ignition coil misfire", confidence="0.78", system="engine",
                   recommended_action="inspect/replace cyl2 coil"),))
    p = build_handoff(case_id="c1", vehicle=VehicleRef(make="Honda", model="Accord", year="2019"),
                      symptoms=[SymptomEvidence(narrative="shakes at idle")],
                      observations=[DiagnosticObservation(kind="dtc", code="P0302")],
                      diagnosis=diag, mileage="87300")
    assert p.dtcs == ("P0302",) and p.leading_hypothesis.startswith("cyl2")
    assert p.requested_service == "inspect/replace cyl2 coil" and p.packet_id.startswith("rcv1:")
    text = format_handoff(p)
    assert "2019 Honda Accord" in text and "P0302" in text and "87300" in text
    assert "Requested service" in text and "Leading hypothesis" in text


def test_handoff_includes_safety_note():
    diag = Diagnosis(case_id="c1", hypotheses=(
        Hypothesis(cause="brake caliper seizing", confidence="0.6", system="brakes",
                   severity=Severity.HIGH, urgency=Urgency.URGENT),))
    p = build_handoff(case_id="c1", vehicle=VehicleRef(make="Ford"), diagnosis=diag)
    assert p.safety_note and "brake" in format_handoff(p).lower()


def test_outcome_extractor_parses_repair():
    llm = lambda s, u: json.dumps({"diagnosis": "cyl2 misfire", "procedure": "replaced ignition coil",
                                   "part": "ignition coil", "cost": 180, "provenance": "shop",
                                   "fixed": True})
    rep = OutcomeExtractor(llm=llm).extract("shop replaced the coil for $180, runs great now")
    assert rep.fixed is True and rep.part == "ignition coil" and rep.cost == "180"
    ack = format_outcome_ack(rep)
    assert "✅" in ack and "$180" in ack


def test_outcome_not_fixed_ack():
    llm = lambda s, u: json.dumps({"procedure": "replaced plugs", "fixed": False})
    rep = OutcomeExtractor(llm=llm).extract("they replaced the plugs but it still shakes")
    assert rep.fixed is False
    assert "not" in format_outcome_ack(rep).lower()


# ---- service routing (handoff / outcome / diagnosis) ----

class _Ch:
    def __init__(self): self.sent = []
    def poll(self, *, timeout=0): return []
    def send_text(self, cid, t):
        self.sent.append((cid, t)); return type("R", (), {"status": "sent"})()


class _Store:
    def __init__(self): self.outcomes = []
    def append_event(self, e): pass
    def upsert_case(self, *a, **k): pass
    def record_observation(self, *a, **k): pass
    def record_diagnosis(self, *a, **k): pass
    def record_outcome(self, oid, cid, did, repair): self.outcomes.append((oid, cid, did, repair))


def _ev(text):
    return InteractionEvent(interaction_id="i", conversation_id="tg:1", channel=Channel.TELEGRAM,
                            modality=Modality.TEXT, text=text, timestamp="2026-09-07T00:00:00Z")


def test_service_routes_handoff_and_outcome():
    ch, store = _Ch(), _Store()
    diagnoser = Diagnoser(llm=lambda s, u: json.dumps({"hypotheses": [
        {"cause": "cyl2 misfire", "confidence": 0.8, "system": "engine",
         "recommended_action": "replace coil"}]}))
    outcome = OutcomeExtractor(llm=lambda s, u: json.dumps({"procedure": "replaced coil", "fixed": True}))
    svc = DiagnosisService(channel=ch, diagnoser=diagnoser, outcome_extractor=outcome, store=store)

    svc.handle(_ev("engine shakes at idle P0302"))         # diagnosis (sets last_diagnosis)
    r_h = svc.handle(_ev("send this to my shop"))           # handoff
    assert "Diagnostic handoff" in r_h and "Requested service" in r_h
    r_o = svc.handle(_ev("they replaced the coil, it's fixed"))   # outcome → recorded
    assert "✅" in r_o and store.outcomes and store.outcomes[0][3].fixed is True
