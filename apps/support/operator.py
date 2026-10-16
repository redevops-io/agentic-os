"""agentic-support as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive support as a capability operator — the same core Chatwoot
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  support.draft_reply     — draft a reply and post it as a PRIVATE NOTE (human-reviewable)
  support.resolve         — toggle a conversation to resolved
  support.escalate        — set priority urgent + assign to an explicit assignee_id or team_id
  support.send_onboarding — send a welcome message to a newly-onboarded customer

resolve / escalate / send_onboarding are approval_required: resolve and escalate change a customer's
ticket and send_onboarding sends a real outbound message to the customer, so on the ungoverned path they
only STAGE (`pending_approval`, executed False) and act only on a runtime-delivered `_approval`. The same gate
lives INSIDE the core functions, so the app's HTTP routes are covered too (the `gated` wrapper is belt-and-braces).
draft_reply posts a PRIVATE NOTE (human-reviewable, never customer-visible) and stays ungated. The write
actions carry side_effecting=True so the runtime dedupes them exactly-once on the Idempotency-Key.
"""
from __future__ import annotations

from agentic_os.approval_gate import gated
from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_support_operator() -> Operator:
    return Operator("support", [
        capability(
            "support.draft_reply",
            lambda inp: core.draft_reply(inp),
            provides=["reply_drafted"],
            outputs={"reply_drafted": "reply drafted + posted as a private note for human review"},
            side_effecting=True,
            compensation={"supported": False, "disposition": "PRESERVE", "notes": "a drafted reply is kept for the agent to review; it is never sent or auto-removed by a failed workflow"}, permissions=["support:write"],
            estimated_value="high", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="support:conversation:{conversation_id}",
        ),
        capability(
            "support.resolve",
            gated("support.resolve", core.resolve, ticket_resolved=False),
            provides=["ticket_resolved"],
            outputs={"ticket_resolved": "conversation status toggled to resolved in Chatwoot"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "closing a ticket notifies the customer; not recallable"}, approval_required=True, permissions=["support:write"],
            estimated_value="medium", latency_ms=600,
            concurrency_mode="exclusive", concurrency_key="support:conversation:{conversation_id}",
        ),
        capability(
            "support.escalate",
            gated("support.escalate", core.escalate, ticket_escalated=False),
            provides=["ticket_escalated"],
            outputs={"ticket_escalated": "priority set to urgent + assigned to an agent"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "escalation notifies customer/staff; not recallable"}, approval_required=True, permissions=["support:write"],
            estimated_value="high", latency_ms=900,
            concurrency_mode="exclusive", concurrency_key="support:conversation:{conversation_id}",
        ),
        capability(
            "support.send_onboarding",
            gated("support.send_onboarding", core.send_onboarding, onboarding_sent=False),
            provides=["onboarding_sent"],
            outputs={"onboarding_sent": "welcome message read back from the Chatwoot conversation as an outgoing, non-private message (True only after that read-back; channel delivery is Chatwoot's async job and is not confirmed)"},
            side_effecting=True,
            compensation={"supported": False, "reversibility": "IRREVERSIBLE", "notes": "sent email cannot be recalled"}, approval_required=True, permissions=["support:write"],
            estimated_value="medium", latency_ms=1000,
            concurrency_key="provider:chatwoot", max_parallelism=3,  # creates a new conversation; bounded
        ),
    ])
