"""DiagnosisService — the live loop: a driver messages the bot, gets a governed diagnosis back.

Ties the channel (Telegram now, WhatsApp later), Grok speech/vision, the Diagnoser, and the Doris case
store into one flow. Each inbound InteractionEvent is normalized (voice note → STT, photo → vision, VIN
→ vPIC identity, fault codes → observations), accumulated onto the case, and a fresh Diagnosis is
produced and replied. Every seam is injectable, so the whole loop is unit-tested with fakes; ``main``
wires the real deps from env (REDEVOPS_BOT_TOKEN + XAI_API_KEY) for the live bot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from runtime_contracts import InteractionEvent, Modality

from .contracts import Diagnosis, DiagnosticObservation, SymptomEvidence, VehicleRef, Powertrain
from .diagnose import Diagnoser, format_reply
from .review import QuoteReviewer, format_second_opinion, looks_like_quote
from .vin import decode_vin

_DTC_RE = re.compile(r"\b([PBCU][0-9]{4})\b")
_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")   # VIN excludes I,O,Q

# describe_image(image_bytes, mime, prompt) -> short text description. Injectable (default: none).
VisionFn = Callable[[bytes, str, str], str]


@dataclass
class _Case:
    case_id: str
    vehicle: VehicleRef = field(default_factory=VehicleRef)
    symptoms: List[SymptomEvidence] = field(default_factory=list)
    observations: List[DiagnosticObservation] = field(default_factory=list)
    last_diagnosis: Optional[Diagnosis] = None


@dataclass
class DiagnosisService:
    channel: Any                      # TelegramChannelAdapter / WhatsAppChannelAdapter
    diagnoser: Diagnoser
    speech: Any = None                # GrokSpeechProvider (STT); optional
    vision: Optional[VisionFn] = None
    store: Any = None                 # DorisCaseStore; optional
    reviewer: Optional[QuoteReviewer] = None   # second-opinion on shop quotes; optional
    vin_decoder: Callable[[str], VehicleRef] = decode_vin
    _cases: Dict[str, _Case] = field(default_factory=dict)

    def _case(self, cid: str) -> _Case:
        return self._cases.setdefault(cid, _Case(case_id=cid))

    def ingest(self, event: InteractionEvent) -> _Case:
        """Normalize one inbound event onto its case (STT / vision / VIN / DTC extraction)."""
        case = self._case(event.conversation_id)
        text = event.text or ""
        if event.modality is Modality.AUDIO and event.artifact_ref and self.speech is not None:
            try:
                audio, mime = self.channel.fetch_file(event.artifact_ref)
                tr = self.speech.transcribe_audio(audio, media_type=mime)
                text = tr.text
            except Exception:  # noqa: BLE001 — never crash the loop on a media/STT hiccup
                text = text or "[voice note could not be transcribed]"
        elif event.modality is Modality.IMAGE and event.artifact_ref and self.vision is not None:
            try:
                img, mime = self.channel.fetch_file(event.artifact_ref)
                desc = self.vision(img, mime, "Describe any vehicle fault shown (dashboard light, leak, "
                                              "damaged part, fault code).")
                text = (text + " " + desc).strip() if text else f"[photo] {desc}"
            except Exception:  # noqa: BLE001
                text = text or "[image attached]"

        if text:
            case.symptoms.append(SymptomEvidence(narrative=text, observed_at=event.timestamp))
        for code in _DTC_RE.findall(text.upper()):
            case.observations.append(DiagnosticObservation(kind="dtc", code=code, source="user",
                                                           observed_at=event.timestamp))
        # VIN → vehicle identity (once)
        if not case.vehicle.vin:
            for cand in _VIN_RE.findall(text.upper()):
                if any(ch.isdigit() for ch in cand):     # avoid matching 17-letter words
                    case.vehicle = self.vin_decoder(cand)
                    break
        if self.store is not None:
            try:
                self.store.append_event(event)
                if not case.vehicle.vin and case.vehicle.make:
                    pass
            except Exception:  # noqa: BLE001 — persistence is best-effort, never blocks a reply
                pass
        return case

    def diagnose_case(self, case: _Case) -> str:
        diag = self.diagnoser.diagnose(case_id=case.case_id, vehicle=case.vehicle,
                                       symptoms=case.symptoms, observations=case.observations)
        case.last_diagnosis = diag
        if self.store is not None:
            try:
                if case.vehicle.vin:
                    self.store.upsert_case(case.case_id, case.vehicle)
                for o in case.observations:
                    self.store.record_observation(case.case_id, o)
                self.store.record_diagnosis(diag)
            except Exception:  # noqa: BLE001
                pass
        return format_reply(diag)

    def handle(self, event: InteractionEvent) -> str:
        """Ingest one event; if it's a shop quote, give a second opinion, else diagnose. Reply on the
        channel either way."""
        case = self.ingest(event)
        latest = case.symptoms[-1].narrative if case.symptoms else (event.text or "")
        if self.reviewer is not None and looks_like_quote(latest):
            op = self.reviewer.review(case_id=case.case_id, vehicle=case.vehicle, quote_text=latest,
                                      symptoms=case.symptoms[:-1], observations=case.observations,
                                      diagnosis=case.last_diagnosis, created_at=event.timestamp)
            reply = format_second_opinion(op)
        else:
            reply = self.diagnose_case(case)
        try:
            self.channel.send_text(event.conversation_id, reply)
        except Exception:  # noqa: BLE001 — return the reply even if send fails (tests/telemetry)
            pass
        return reply

    def run_once(self, *, timeout: int = 0) -> int:
        """Poll the channel once; handle every inbound event. Returns count handled."""
        events = self.channel.poll(timeout=timeout)
        for ev in events:
            self.handle(ev)
        return len(events)


def main() -> None:  # pragma: no cover - live loop
    import os
    import time
    from agentic_os.interaction import TelegramChannelAdapter, GrokSpeechProvider  # noqa: WPS433

    channel = TelegramChannelAdapter()            # REDEVOPS_BOT_TOKEN
    speech = GrokSpeechProvider()                 # XAI_API_KEY
    llm = _grok_llm()
    diagnoser = Diagnoser(llm=llm)
    reviewer = QuoteReviewer(llm=llm)
    store = None
    if os.environ.get("CAR_DORIS_HOST"):
        from .store_doris import DorisCaseStore
        store = DorisCaseStore()
    svc = DiagnosisService(channel=channel, diagnoser=diagnoser, reviewer=reviewer, speech=speech,
                           vision=_grok_vision(), store=store)
    me = channel.get_me()
    print(f"diagnosis bot live as @{me.get('username')} — polling")
    while True:
        try:
            svc.run_once(timeout=30)
        except Exception as e:  # noqa: BLE001
            print("loop error:", type(e).__name__, str(e)[:120]); time.sleep(3)


def _grok_llm():  # pragma: no cover - live
    import json as _json
    import os
    import urllib.request

    def call(system: str, user: str) -> str:
        body = _json.dumps({"model": "grok-4.20-0309-non-reasoning",
                            "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}],
                            "temperature": 0.3}).encode()
        req = urllib.request.Request("https://api.x.ai/v1/chat/completions", method="POST", data=body,
                                     headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = _json.loads(r.read())
        return d["choices"][0]["message"]["content"] or ""
    return call


def _grok_vision():  # pragma: no cover - live
    import base64
    import json as _json
    import os
    import urllib.request

    def describe(image: bytes, mime: str, prompt: str) -> str:
        data_uri = f"data:{mime};base64," + base64.b64encode(image).decode()
        body = _json.dumps({"model": "grok-4.6",
                            "messages": [{"role": "user", "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": data_uri}}]}],
                            "temperature": 0.2, "max_tokens": 300}).encode()
        req = urllib.request.Request("https://api.x.ai/v1/chat/completions", method="POST", data=body,
                                     headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = _json.loads(r.read())
        return d["choices"][0]["message"]["content"] or ""
    return describe


if __name__ == "__main__":  # pragma: no cover
    main()
