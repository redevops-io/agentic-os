"""Collaboration connectors — chat surfaces as event + conversation + action planes (plan §6).

Slack / Microsoft Teams / Google Chat are modelled as a SINGLE contract (`CollaborationProvider`) so workflow
business logic is independent of the trigger/delivery surface: moving Slack → Teams must not require rewriting
the workflow (§6.5 channel portability). Inbound events normalize to one `InboundEvent` shape; outbound actions
go through one interface. Each adapter is credential-gated and offline-testable via an injected transport.
"""
from .contracts import (
    Attachment, CollaborationProvider, InboundEvent, InteractionResult, OutboundMessage,
)
from .slack import SlackCollaborationProvider

__all__ = [
    "CollaborationProvider", "InboundEvent", "OutboundMessage", "InteractionResult", "Attachment",
    "SlackCollaborationProvider",
]
