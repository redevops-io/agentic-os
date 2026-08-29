"""agentic-support as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive support as a capability operator — the same core Chatwoot
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  support.draft_reply     — draft a reply and post it as a PRIVATE NOTE (human-reviewable)
  support.resolve         — toggle a conversation to resolved
  support.escalate        — set priority urgent + assign to an agent
  support.send_onboarding — send a welcome message to a newly-onboarded customer

agentic-support has NO approval gates (modules.yaml `approval_required: []`), so none of
the capabilities are approval_required. The write actions carry side_effecting=True so the
runtime dedupes them exactly-once on the Idempotency-Key.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_support_operator() -> Operator:
    return Operator("support", [
        capability(
            "support.draft_reply",
            lambda inp: core.draft_reply(inp),
            provides=["reply_drafted"],
            outputs={"reply_drafted": "reply drafted + posted as a private note for human review"},
            side_effecting=True, permissions=["support:write"],
            estimated_value="high", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="support:conversation:{conversation_id}",
        ),
        capability(
            "support.resolve",
            lambda inp: core.resolve(inp),
            provides=["ticket_resolved"],
            outputs={"ticket_resolved": "conversation status toggled to resolved in Chatwoot"},
            side_effecting=True, permissions=["support:write"],
            estimated_value="medium", latency_ms=600,
            concurrency_mode="exclusive", concurrency_key="support:conversation:{conversation_id}",
        ),
        capability(
            "support.escalate",
            lambda inp: core.escalate(inp),
            provides=["ticket_escalated"],
            outputs={"ticket_escalated": "priority set to urgent + assigned to an agent"},
            side_effecting=True, permissions=["support:write"],
            estimated_value="high", latency_ms=900,
            concurrency_mode="exclusive", concurrency_key="support:conversation:{conversation_id}",
        ),
        capability(
            "support.send_onboarding",
            lambda inp: core.send_onboarding(inp),
            provides=["onboarding_sent"],
            outputs={"onboarding_sent": "welcome message delivered to the new customer in Chatwoot"},
            side_effecting=True, permissions=["support:write"],
            estimated_value="medium", latency_ms=1000,
            concurrency_key="provider:chatwoot", max_parallelism=3,  # creates a new conversation; bounded
        ),
    ])
