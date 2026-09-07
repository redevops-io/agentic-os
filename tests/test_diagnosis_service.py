"""DiagnosisService — the live loop, wired with fakes (channel / speech / vision / diagnoser / store)."""
from __future__ import annotations

import json

from runtime_contracts import Channel, InteractionEvent, Modality
from agentic_os.automotive import Diagnoser, DiagnosisService, VehicleRef


class _FakeChannel:
    def __init__(self, files=None, inbound=None):
        self.sent = []                      # (conversation_id, text)
        self._files = files or {}           # file_id -> (bytes, mime)
        self._inbound = inbound or []
    def poll(self, *, timeout=0):
        d, self._inbound = self._inbound, []
        return d
    def fetch_file(self, file_id):
        return self._files[file_id]
    def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        class _R: status = "sent"
        return _R()


class _FakeSpeech:
    def __init__(self, text):
        self._text = text
    def transcribe_audio(self, audio, *, media_type="", language=None):
        class _T:
            text = self._text
        return _T()


def _llm(hyps, requests=None):
    return lambda s, u: json.dumps({"hypotheses": hyps, "evidence_requests": requests or []})


def _ev(**kw):
    base = dict(interaction_id="i1", conversation_id="tg:42", channel=Channel.TELEGRAM,
                modality=Modality.TEXT, participant_ref="42", timestamp="2026-09-07T00:00:00Z")
    base.update(kw)
    return InteractionEvent(**base)


def test_text_message_flows_to_a_diagnosis_reply():
    ch = _FakeChannel()
    svc = DiagnosisService(channel=ch, diagnoser=Diagnoser(llm=_llm(
        [{"cause": "dead 12V battery", "confidence": 0.82, "system": "electrical",
          "recommended_action": "test the battery"}])))
    reply = svc.handle(_ev(text="my car won't start, just clicks"))
    assert "dead 12V battery" in reply and "82%" in reply
    assert ch.sent and ch.sent[0][0] == "tg:42"      # replied on the same conversation


def test_voice_note_is_transcribed_before_diagnosis():
    ch = _FakeChannel(files={"VOICE1": (b"ogg", "audio/ogg")})
    svc = DiagnosisService(channel=ch, speech=_FakeSpeech("it shakes at idle and the light is flashing"),
                           diagnoser=Diagnoser(llm=_llm(
                               [{"cause": "misfire", "confidence": 0.7, "system": "engine"}])))
    case = svc.ingest(_ev(modality=Modality.AUDIO, artifact_ref="VOICE1", text=""))
    assert case.symptoms and "shakes at idle" in case.symptoms[0].narrative


def test_image_uses_vision_description():
    ch = _FakeChannel(files={"IMG1": (b"jpg", "image/jpeg")})
    svc = DiagnosisService(channel=ch, vision=lambda b, m, p: "red oil-pressure warning light on",
                           diagnoser=Diagnoser(llm=_llm([{"cause": "low oil pressure", "confidence": 0.6,
                                                          "system": "engine"}])))
    case = svc.ingest(_ev(modality=Modality.IMAGE, artifact_ref="IMG1", text=""))
    assert "oil-pressure warning" in case.symptoms[0].narrative


def test_dtc_and_vin_extracted_from_text():
    ch = _FakeChannel()
    svc = DiagnosisService(channel=ch, diagnoser=Diagnoser(llm=_llm([])),
                           vin_decoder=lambda vin: VehicleRef(vin=vin, make="Honda", model="Accord",
                                                              year="2019"))
    case = svc.ingest(_ev(text="my VIN is 1HGCM82633A004352 and it shows P0302"))
    assert any(o.code == "P0302" for o in case.observations)
    assert case.vehicle.make == "Honda" and case.vehicle.vin == "1HGCM82633A004352"


def test_run_once_handles_all_polled_events():
    ch = _FakeChannel(inbound=[_ev(text="won't start"), _ev(interaction_id="i2", text="clicking noise")])
    svc = DiagnosisService(channel=ch, diagnoser=Diagnoser(llm=_llm(
        [{"cause": "starter", "confidence": 0.75, "system": "electrical"}])))
    n = svc.run_once()
    assert n == 2 and len(ch.sent) == 2


def test_store_writes_are_best_effort_never_break_reply():
    class _BoomStore:
        def append_event(self, e): raise RuntimeError("doris down")
        def upsert_case(self, *a, **k): raise RuntimeError("doris down")
        def record_observation(self, *a, **k): raise RuntimeError("doris down")
        def record_diagnosis(self, *a, **k): raise RuntimeError("doris down")
    ch = _FakeChannel()
    svc = DiagnosisService(channel=ch, store=_BoomStore(), diagnoser=Diagnoser(llm=_llm(
        [{"cause": "alternator", "confidence": 0.7, "system": "electrical"}])))
    reply = svc.handle(_ev(text="battery light on"))    # store raises internally, reply still returns
    assert "alternator" in reply
