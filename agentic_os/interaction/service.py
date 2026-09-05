"""InteractionService — the conversation-side logic behind the interaction operator.

Receives inbound InteractionEvents from channel adapters, transcribes audio through a SpeechProvider
(recording the transcript as a DERIVED event bound to the source audio by hash), replies through the
right channel adapter, and packages a human handoff. Everything is recorded in the append-only
``ConversationStore`` so a phone call, a Slack approval and a WhatsApp follow-up accrue to the SAME
conversation. Providers are duck-typed (the ``runtime_contracts`` seams); the offline stubs or a real
NeMo adapter both satisfy them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from runtime_contracts import (
    Channel,
    DeliveryReceipt,
    InteractionEvent,
    MediaArtifact,
    Modality,
    TranscriptArtifact,
)

from .conversation import ConversationStore


@dataclass(frozen=True)
class HandoffPackage:
    """What a human needs to take over a conversation — references only, never re-inlined content."""

    conversation_id: str
    to: str                                   # target human/queue (e.g. "slack:#support")
    channels: Tuple[str, ...]
    interaction_ids: Tuple[str, ...]
    note: str = ""

    def summary(self) -> Dict[str, Any]:
        return {"conversation_id": self.conversation_id, "to": self.to,
                "channels": list(self.channels), "interactions": list(self.interaction_ids),
                "note": self.note}


class InteractionService:
    def __init__(self, store: ConversationStore, *, adapters: Mapping[Channel, Any],
                 speech: Any) -> None:
        self._store = store
        self._adapters = dict(adapters)
        self._speech = speech

    @property
    def store(self) -> ConversationStore:
        return self._store

    def ingest(self, event: InteractionEvent) -> InteractionEvent:
        """Record one inbound interaction."""
        return self._store.append(event)

    def poll(self, channel: Channel) -> List[InteractionEvent]:
        """Drain a channel adapter's inbound queue and record each event."""
        adapter = self._adapter(channel)
        return [self.ingest(e) for e in adapter.receive()]

    def transcribe(self, media: MediaArtifact, *, conversation_id: str, participant_ref: str = "",
                   language: str = "") -> TranscriptArtifact:
        """Transcribe audio → TranscriptArtifact (derived evidence), recorded as a TEXT interaction
        that references the transcript. The transcript stays bound to the source audio by hash."""
        transcript = self._speech.transcribe(media, language=language)
        derived = InteractionEvent(
            interaction_id=f"{conversation_id}:txt:{transcript.artifact_id}",
            conversation_id=conversation_id, channel=Channel.PHONE, modality=Modality.TEXT,
            participant_ref=participant_ref, artifact_ref=transcript.content_hash,
            derived_artifact_refs=(media.media_hash,), text=transcript.text,
            confidence=transcript.confidence, provenance="speech-provider",
        )
        self._store.append(derived)
        return transcript

    def reply(self, conversation_id: str, channel: Channel, *, text: Optional[str] = None,
              media: Optional[MediaArtifact] = None) -> DeliveryReceipt:
        """Send an outbound response over a channel adapter."""
        adapter = self._adapter(channel)
        if media is not None:
            return adapter.send_audio(conversation_id, media)
        return adapter.send_text(conversation_id, text or "")

    def handoff(self, conversation_id: str, *, to: str, note: str = "") -> HandoffPackage:
        """Bundle the conversation for a human takeover — references, not re-inlined content."""
        events = self._store.for_conversation(conversation_id)
        return HandoffPackage(
            conversation_id=conversation_id, to=to,
            channels=tuple(c.value for c in self._store.channels(conversation_id)),
            interaction_ids=tuple(e.interaction_id for e in events), note=note,
        )

    def _adapter(self, channel: Channel) -> Any:
        adapter = self._adapters.get(channel)
        if adapter is None:
            raise KeyError(f"no channel adapter registered for {channel.value}")
        return adapter
