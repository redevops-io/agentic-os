"""RAG-augmented Diagnoser — retrieved corpus docs ground the prompt (offline)."""
from __future__ import annotations

import json

from agentic_os.automotive import (
    Diagnoser,
    DiagnosticObservation,
    SymptomEvidence,
    VehicleRef,
    doris_retriever,
)


class _RecordingLLM:
    def __init__(self, obj): self.obj = obj; self.last_user = None
    def __call__(self, system, user): self.last_user = user; return json.dumps(self.obj)


def _hyp(): return {"hypotheses": [{"cause": "cyl2 misfire", "confidence": 0.8, "system": "engine"}]}


def test_retriever_grounds_the_prompt():
    llm = _RecordingLLM(_hyp())
    refs = [{"dtc": "P0302", "title": "Cylinder 2 Misfire"},
            {"dtc": "P1302", "title": "Cylinder 2 Misfire"},        # dup title → deduped
            {"dtc": "P0300", "title": "Random/Multiple Cylinder Misfire"}]
    d = Diagnoser(llm=llm, retriever=lambda q, k: refs, retrieve_k=2)
    d.diagnose(case_id="c1", vehicle=VehicleRef(make="Honda"),
               observations=[DiagnosticObservation(kind="dtc", code="P0302")],
               symptoms=[SymptomEvidence(narrative="shakes at idle")])
    assert "Reference knowledge" in llm.last_user
    assert "P0302: Cylinder 2 Misfire" in llm.last_user
    assert "P0300" not in llm.last_user          # capped at retrieve_k=2 after dedup


def test_retriever_query_uses_dtcs_and_symptoms():
    seen = {}
    def retr(q, k): seen["q"] = q; return []
    Diagnoser(llm=_RecordingLLM(_hyp()), retriever=retr).diagnose(
        case_id="c1", vehicle=VehicleRef(),
        observations=[DiagnosticObservation(kind="dtc", code="P0302")],
        symptoms=[SymptomEvidence(narrative="rough idle")])
    assert "P0302" in seen["q"] and "rough idle" in seen["q"]


def test_no_retriever_is_fine():
    llm = _RecordingLLM(_hyp())
    Diagnoser(llm=llm).diagnose(case_id="c1", vehicle=VehicleRef(make="Kia"))
    assert "Reference knowledge" not in llm.last_user


def test_retriever_failure_never_breaks_diagnosis():
    def boom(q, k): raise RuntimeError("doris down")
    d = Diagnoser(llm=_RecordingLLM(_hyp()), retriever=boom)
    diag = d.diagnose(case_id="c1", vehicle=VehicleRef(make="Ford"),
                      symptoms=[SymptomEvidence(narrative="x")])
    assert diag.top.cause == "cyl2 misfire"       # still diagnoses despite retriever error


def test_doris_retriever_factory():
    calls = {}
    class _Store:
        def knowledge_search(self, vec, *, k=5, dtc=""):
            calls["vec_len"] = len(vec); calls["k"] = k
            return [{"dtc": "P0302", "title": "Cylinder 2 Misfire"}]
    r = doris_retriever(lambda texts: [[0.1, 0.2, 0.3]], _Store())
    out = r("cylinder 2 misfire", 5)
    assert calls["vec_len"] == 3 and calls["k"] == 5 and out[0]["dtc"] == "P0302"
