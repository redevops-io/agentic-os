"""NeMo Voice Agent → ReDevOps interaction wiring (the real VoiceSessionProvider).

The deployed NVIDIA NeMo Voice Agent (see ``deploy/nemo-voice-agent/``) runs the audio pipeline —
streaming ASR, diarization, the LLM, and TTS — over one Pipecat WebSocket, and exposes the running
conversation as an LLM *context*: a list of ``{"role", "content"}`` messages (system / user / assistant),
retrievable live via the RTVI ``get_context_history`` action. This module turns that context into the
Runtime's own ``InteractionEvent`` stream, so a spoken customer turn becomes a governed Mission input —
one conversation, attributable, replayable — with the audio I/O and turn-taking left to NeMo.

Division of labour: NeMo owns the *voice session* (bytes, VAD, endpointing, barge-in); ReDevOps owns
what was *said and meant*. The audio-frame transport is the Pipecat RTVI protobuf protocol on the WS
(exercised end-to-end by the repo's two-bot evaluation harness); the piece that matters to the Runtime —
projecting the transcript into content-addressed InteractionEvents — is pure and tested here offline.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from runtime_contracts import Channel, InteractionEvent, Modality

from .conversation import ConversationStore

# role → who produced the utterance; "system" is prompt scaffolding, never an interaction.
_USER_ROLES = {"user"}
_AGENT_ROLES = {"assistant", "bot"}


def _coerce_context(context: Union[str, Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    """NeMo returns the context either as a real list or as its ``repr`` string (single-quoted, the
    form ``get_context_history`` sends on the wire). Accept both; never execute it."""
    if isinstance(context, str):
        try:
            return list(json.loads(context))
        except ValueError:
            try:
                return list(ast.literal_eval(context))   # single-quoted python-repr form
            except (ValueError, SyntaxError):
                return []
    return [dict(m) for m in context]


def interaction_events_from_context(
    context: Union[str, Sequence[Mapping[str, Any]]], *, conversation_id: str,
    channel: Channel = Channel.PHONE, participant_ref: str = "",
) -> List[InteractionEvent]:
    """Project a NeMo LLM context (the transcript) into InteractionEvents, in order.

    User turns become inbound AUDIO-derived TEXT interactions (what the customer said); agent turns
    become interactions with ``provenance="nemo-agent"`` (what was spoken back). System scaffolding is
    skipped. Each event is content-addressed, so re-projecting the same transcript is stable.
    """
    events: List[InteractionEvent] = []
    turn = 0
    for msg in _coerce_context(context):
        role = str(msg.get("role", "")).lower()
        text = str(msg.get("content") or "").strip()
        if not text or role not in _USER_ROLES | _AGENT_ROLES:
            continue
        turn += 1
        is_user = role in _USER_ROLES
        events.append(InteractionEvent(
            interaction_id=f"{conversation_id}:{turn}:{role}",
            conversation_id=conversation_id, channel=channel, modality=Modality.TEXT,
            participant_ref=participant_ref if is_user else "",
            text=text,
            provenance="nemo-asr" if is_user else "nemo-agent",
        ))
    return events


@dataclass
class NeMoSession:
    conversation_id: str
    channel: Channel


class NeMoVoiceAgentProvider:
    """A ``VoiceSessionProvider`` backed by a deployed NeMo Voice Agent.

    Duck-types the ``runtime_contracts`` ``VoiceSessionProvider`` seam. The audio session itself is a
    Pipecat RTVI WebSocket the NeMo server serves at ``ws_url``; this adapter's job is to take the
    session's transcript/context and record it as InteractionEvents on the conversation store, so a
    voice call flows into the same governed path as any other channel.
    """

    def __init__(self, *, ws_url: str = "ws://localhost:8765",
                 http_url: str = "http://localhost:7860", channel: Channel = Channel.PHONE,
                 store: Optional[ConversationStore] = None) -> None:
        self.ws_url = ws_url
        self.http_url = http_url
        self.channel = channel
        self.store = store if store is not None else ConversationStore()

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider": "nvidia-nemo-voice-agent",
            "transport": "pipecat-rtvi-websocket",
            "ws_url": self.ws_url,
            "modalities": [Modality.AUDIO.value, Modality.TEXT.value],
            "streaming_asr": True, "diarization": True, "tts": True,
        }

    def start_session(self, conversation_id: str, *, config: Optional[Mapping[str, Any]] = None) -> NeMoSession:
        return NeMoSession(conversation_id=conversation_id, channel=self.channel)

    def ingest_context(self, session: NeMoSession,
                       context: Union[str, Sequence[Mapping[str, Any]]], *,
                       participant_ref: str = "") -> List[InteractionEvent]:
        """Project the session's NeMo transcript into InteractionEvents and record them."""
        events = interaction_events_from_context(
            context, conversation_id=session.conversation_id, channel=session.channel,
            participant_ref=participant_ref)
        for e in events:
            self.store.append(e)
        return events

    def events(self, session: NeMoSession) -> List[InteractionEvent]:
        return self.store.for_conversation(session.conversation_id)

    def end_session(self, session: NeMoSession) -> None:  # the WS lifecycle is NeMo's; nothing to hold
        return None
