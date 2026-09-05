"""ConversationStore — an append-only log of InteractionEvents, keyed by conversation.

One conversation can span many channels (a phone call, then a Slack approval, then a WhatsApp
follow-up), and every one of them appends here, so cross-channel continuity is just "same
conversation_id". Deliberately tiny and reload-friendly, mirroring the mission ``EventStore``: the
durable record of what was said/received, never a place to mutate history.
"""
from __future__ import annotations

from typing import Dict, List

from runtime_contracts import Channel, InteractionEvent


class ConversationStore:
    """Append-only per-conversation InteractionEvent log."""

    def __init__(self) -> None:
        self._events: List[InteractionEvent] = []

    def append(self, event: InteractionEvent) -> InteractionEvent:
        self._events.append(event)
        return event

    def for_conversation(self, conversation_id: str) -> List[InteractionEvent]:
        return [e for e in self._events if e.conversation_id == conversation_id]

    def all(self) -> List[InteractionEvent]:
        return list(self._events)

    def conversations(self) -> List[str]:
        seen: List[str] = []
        for e in self._events:
            if e.conversation_id not in seen:
                seen.append(e.conversation_id)
        return seen

    def channels(self, conversation_id: str) -> List[Channel]:
        seen: List[Channel] = []
        for e in self.for_conversation(conversation_id):
            if e.channel not in seen:
                seen.append(e.channel)
        return seen
