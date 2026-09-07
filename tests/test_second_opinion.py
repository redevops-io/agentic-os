"""P2 — second-opinion on a shop quote: consistency check + questions to ask before authorizing."""
from __future__ import annotations

import json

from runtime_contracts import Channel, InteractionEvent, Modality
from agentic_os.automotive import (
    ConsistencyVerdict,
    Diagnoser,
    Diagnosis,
    DiagnosisService,
    DiagnosticObservation,
    Hypothesis,
    QuoteReviewer,
    SymptomEvidence,
    VehicleRef,
    format_second_opinion,
    looks_like_quote,
)


def _llm(obj):
    return lambda s, u: json.dumps(obj)


def test_looks_like_quote_heuristic():
    assert looks_like_quote("Shop quote: replace transmission, $3200 labor 6h")
    assert looks_like_quote("/quote they said i need a new alternator for $600")
    assert not looks_like_quote("my car makes a grinding noise when I brake")
    assert not looks_like_quote("")


def test_reviewer_flags_inconsistent_repair():
    llm = _llm({"verdict": "inconsistent",
                "reasons": ["Quote replaces the transmission, but the evidence points to a misfire",
                            "No test of the ignition coil mentioned"],
                "questions": ["Why replace the transmission for a P0302 misfire?",
                              "Did you test cylinder 2's coil first?"],
                "proposed": {"description": "replace transmission", "cost": 3200, "labor_hours": 6}})
    op = QuoteReviewer(llm=llm).review(
        case_id="c1", vehicle=VehicleRef(make="Honda", model="Accord"),
        quote_text="Quote: replace transmission $3200",
        observations=[DiagnosticObservation(kind="dtc", code="P0302")],
        diagnosis=Diagnosis(case_id="c1", hypotheses=(Hypothesis(cause="cyl2 misfire", confidence="0.8"),)))
    assert op.verdict is ConsistencyVerdict.INCONSISTENT
    assert op.proposed.cost == "3200" and len(op.questions) >= 2
    r = format_second_opinion(op)
    assert "⚠️" in r and "transmission" in r.lower() and "Ask the shop" in r


def test_reviewer_reasonable_still_gives_questions():
    llm = _llm({"verdict": "reasonable", "reasons": ["Matches the P0302 misfire diagnosis"],
                "questions": [], "proposed": {"description": "replace cyl2 coil", "cost": 250}})
    op = QuoteReviewer(llm=llm).review(case_id="c1", vehicle=VehicleRef(make="Honda"),
                                       quote_text="replace ignition coil $250")
    assert op.verdict is ConsistencyVerdict.REASONABLE
    assert op.questions   # never rubber-stamp — always at least one question
    assert "✅" in format_second_opinion(op)


def test_reviewer_content_addressed():
    op = QuoteReviewer(llm=_llm({"verdict": "unclear", "reasons": [], "questions": ["ask x"]})).review(
        case_id="c1", vehicle=VehicleRef(), quote_text="estimate $500")
    assert op.opinion_id.startswith("rcv1:")


# ---- service routing ----

class _FakeChannel:
    def __init__(self): self.sent = []
    def poll(self, *, timeout=0): return []
    def send_text(self, cid, text):
        self.sent.append((cid, text))
        class _R: status = "sent"
        return _R()


def _ev(text):
    return InteractionEvent(interaction_id="i", conversation_id="tg:1", channel=Channel.TELEGRAM,
                            modality=Modality.TEXT, text=text, timestamp="2026-09-07T00:00:00Z")


def test_service_routes_symptom_to_diagnosis_and_quote_to_second_opinion():
    ch = _FakeChannel()
    diagnoser = Diagnoser(llm=_llm({"hypotheses": [
        {"cause": "cyl2 misfire", "confidence": 0.8, "system": "engine"}]}))
    reviewer = QuoteReviewer(llm=_llm({"verdict": "inconsistent",
                                       "reasons": ["evidence says misfire, quote says transmission"],
                                       "questions": ["why the transmission?"], "proposed": {}}))
    svc = DiagnosisService(channel=ch, diagnoser=diagnoser, reviewer=reviewer)

    r1 = svc.handle(_ev("engine shakes at idle, P0302"))      # symptom → diagnosis
    assert "What's most likely" in r1 and "misfire" in r1.lower()
    r2 = svc.handle(_ev("The shop quote says replace transmission for $3200"))  # quote → second opinion
    assert "may not add up" in r2 or "⚠️" in r2
    assert "why the transmission" in r2.lower()
    assert len(ch.sent) == 2
