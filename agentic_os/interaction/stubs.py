"""Offline stubs for the channel/speech seams — no channel or speech SDK required.

These implement the ``runtime_contracts`` ``ChannelAdapter`` / ``SpeechProvider`` contracts with
deterministic, in-memory behaviour so the whole interaction→Mission path runs in a unit test. A real
NeMo Voice Agent adapter or NeMo-Speech.cpp provider drops into the identical seam later; nothing
downstream knows the difference. The stub speech provider does NOT invent a transcription — it returns
the text it was told to associate with a media hash (or a marker), because the point being exercised
is the *binding* (transcript pinned to source audio + model/version), not real ASR.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from runtime_contracts import (
    Channel,
    DeliveryReceipt,
    InteractionEvent,
    MediaArtifact,
    Modality,
    SpeechSegment,
    TranscriptArtifact,
)


class StubChannelAdapter:
    """A channel adapter that replays a fixed inbound queue and records everything it sends."""

    def __init__(self, channel: Channel, *, inbound: Optional[List[InteractionEvent]] = None) -> None:
        self.channel = channel
        self._inbound = list(inbound or [])
        self.sent: List[DeliveryReceipt] = []
        self.acked: List[str] = []

    def receive(self) -> List[InteractionEvent]:
        drained, self._inbound = self._inbound, []
        return drained

    def _receipt(self, conversation_id: str, status: str = "sent") -> DeliveryReceipt:
        r = DeliveryReceipt(interaction_id=f"{conversation_id}:out:{len(self.sent) + 1}",
                            channel=self.channel, status=status,
                            provider_ref=f"stub-{self.channel.value}-{len(self.sent) + 1}")
        self.sent.append(r)
        return r

    def send_text(self, conversation_id: str, text: str) -> DeliveryReceipt:
        return self._receipt(conversation_id)

    def send_audio(self, conversation_id: str, media: MediaArtifact) -> DeliveryReceipt:
        return self._receipt(conversation_id)

    def send_file(self, conversation_id: str, media: MediaArtifact) -> DeliveryReceipt:
        return self._receipt(conversation_id)

    def acknowledge(self, interaction_id: str) -> None:
        self.acked.append(interaction_id)

    def capabilities(self) -> Mapping[str, Any]:
        return {"channel": self.channel.value,
                "modalities": [Modality.TEXT.value, Modality.AUDIO.value]}


class StubSpeechProvider:
    """A deterministic SpeechProvider: canned transcripts keyed by source-media hash."""

    def __init__(self, *, transcripts: Optional[Mapping[str, str]] = None,
                 asr_model: str = "stub-asr", asr_version: str = "0") -> None:
        self._transcripts = dict(transcripts or {})
        self.asr_model = asr_model
        self.asr_version = asr_version

    def transcribe(self, media: MediaArtifact, *, language: str = "") -> TranscriptArtifact:
        text = self._transcripts.get(media.media_hash, "[stub transcript]")
        return TranscriptArtifact(
            artifact_id=f"tr:{media.artifact_id}",
            source_media_hash=media.media_hash,
            segments=(SpeechSegment(text=text, start_ms=0, end_ms=media.duration_ms,
                                    confidence="0.99"),),
            asr_model=self.asr_model, asr_version=self.asr_version, language=language,
            confidence="0.99",
        )

    def synthesize(self, text: str, *, voice: str = "") -> MediaArtifact:
        # a deterministic, content-addressed placeholder for the synthesized audio
        h = f"sha256:tts-{abs(hash((text, voice))) & 0xffffffff:08x}"
        return MediaArtifact(artifact_id=f"tts:{h[-8:]}", modality=Modality.AUDIO,
                             media_hash=h, media_type="audio/wav")

    def capabilities(self) -> Mapping[str, Any]:
        return {"asr": True, "tts": True, "model": self.asr_model, "version": self.asr_version}
