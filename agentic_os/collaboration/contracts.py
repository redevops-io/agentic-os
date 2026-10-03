"""The common collaboration contract (plan §6.1).

One normalized inbound event, one outbound-action interface, across Slack / Teams / Google Chat. The normalized
``InboundEvent`` carries provider / workspace / channel / thread / message / actor / text / mentions /
attachments / timestamp and a ``resource`` id for the per-node tenancy axis — so a workflow trigger and its
delivery read the same shape regardless of surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str = ""
    mime: str = ""


@dataclass(frozen=True)
class InboundEvent:
    """A normalized inbound collaboration event (a message / mention / interaction)."""
    provider: str                               # "slack" | "msteams" | "google_chat"
    workspace: str                              # Slack team / Teams tenant / Google customer
    channel: str                                # channel / conversation / space id
    actor: str                                  # the sending user id
    text: str = ""
    thread: str = ""                            # thread/root id ("" = top-level)
    message_id: str = ""
    mentions: tuple[str, ...] = ()              # user ids mentioned (e.g. the bot)
    attachments: tuple[Attachment, ...] = ()
    ts: str = ""                                # provider timestamp
    resource: str = ""                          # resource id → owning tenant (per-node ExecutionContext)
    raw: dict = field(default_factory=dict)     # provider-native payload (for debugging/replay)


@dataclass(frozen=True)
class OutboundMessage:
    channel: str
    text: str
    thread: str = ""                            # reply in-thread when set
    blocks: tuple = ()                          # provider-native rich blocks/cards (opaque here)


@dataclass(frozen=True)
class InteractionResult:
    """Result of an interactive prompt (e.g. an approval button) posted to a channel."""
    ok: bool
    message_id: str = ""
    detail: str = ""


@runtime_checkable
class CollaborationProvider(Protocol):
    """Event + conversation + action surface. Adapters are credential-gated: ``entitled()`` is False without a
    token, so the open stack runs without the connector. Outbound methods return the provider message id."""
    provider: str

    def entitled(self) -> bool: ...

    # inbound
    def normalize_event(self, raw: dict) -> InboundEvent: ...
    def read_thread(self, channel: str, thread: str, *, limit: int = 50) -> list[InboundEvent]: ...

    # outbound / actions
    def post_message(self, msg: OutboundMessage) -> str: ...
    def reply_thread(self, channel: str, thread: str, text: str) -> str: ...
    def update_message(self, channel: str, message_id: str, text: str) -> str: ...
    def add_reaction(self, channel: str, message_id: str, emoji: str) -> bool: ...
    def upload_file(self, channel: str, name: str, content: bytes, *, thread: str = "") -> str: ...

    # resolution + interaction
    def resolve_user(self, handle: str) -> str: ...
    def resolve_channel(self, name: str) -> str: ...
    def open_interaction(self, channel: str, prompt: str, *, actions: tuple = ()) -> InteractionResult: ...
