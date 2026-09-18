"""Owner Attention Gateway — one concise mobile decision, on the owner's channel.

A Revenue Mission parks at the customer-contact gate (`revenue.send_followup`). The gateway turns that
parked node into a concise, act-without-opening-the-CRM brief, delivers it on the owner's channel
(WhatsApp/Slack/Telegram/Discord/email), and maps the owner's one-tap action — or a pre-authorized
auto-follow-up policy — back into the Mission's approve/reject. Every resolution is a governed Mission
event with its authorizing evidence (which policy, or which owner action).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from .contracts import Disposition, RevenueOpportunity, SendMode
from .policy import AutoFollowupPolicy

# The owner-facing actions the plan mandates.
ALLOWED_ACTIONS = ("APPROVE_AND_SEND", "EDIT", "CALL", "ASSIGN", "SNOOZE",
                   "REQUEST_MORE_CONTEXT", "REJECT", "CLOSE_LOST", "MARK_DONE")


class AttentionChannel(Protocol):
    """Any owner channel that can deliver a brief. Mirrors `channels.py`'s `send`."""
    name: str
    def send(self, text: str) -> None: ...


@dataclass
class RecordingChannel:
    """A channel that records what would be delivered — used offline and in tests, and as the shape a
    real WhatsApp/Slack/Telegram/Discord/SMS adapter fills."""
    name: str = "recording"
    sent: list[str] = field(default_factory=list)

    def send(self, text: str) -> None:
        self.sent.append(text)


@dataclass
class AttentionBrief:
    mission_id: str
    opportunity_id: str
    priority: str
    text: str
    allowed_actions: tuple[str, ...]
    approval_policy: str


def build_brief(opp: RevenueOpportunity, mission_id: str, approval_policy: str) -> AttentionBrief:
    """The concise mobile brief — enough context to act without opening the CRM (plan §2)."""
    lines = [
        f"New {opp.type.value.replace('_', ' ').lower()} — {opp.priority.value} priority",
        f"{opp.contact_name}{', ' + opp.company if opp.company else ''}".strip(", "),
        opp.summary,
    ]
    if opp.estimated_value:
        lines.append(f"Est. value: ${opp.estimated_value:,.0f}")
    if opp.deadline:
        lines.append(f"Deadline: {opp.deadline}")
    if opp.proposed_response:
        lines.append(f"Suggested response: “{opp.proposed_response}”")
    lines.append("[Approve & Send] [Edit] [Call] [Snooze] [Assign] [Dismiss]")
    return AttentionBrief(mission_id=mission_id, opportunity_id=opp.opportunity_id,
                          priority=opp.priority.value, text="\n".join(lines),
                          allowed_actions=ALLOWED_ACTIONS, approval_policy=approval_policy)


@dataclass
class SendResolution:
    mode: SendMode
    resolved: bool                 # True if the send gate was resolved (approved) here
    by: str                        # "policy" | "owner:<action>" | "pending"
    reason: str
    brief: Optional[AttentionBrief] = None


class OwnerAttentionGateway:
    """Bridges a parked Revenue Mission send-gate to the owner's channel and back."""

    def __init__(self, channel: Optional[AttentionChannel] = None):
        self.channel: AttentionChannel = channel or RecordingChannel()

    def request_send(self, rt, mission_id: str, opp: RevenueOpportunity, *,
                     policy: AutoFollowupPolicy, message_class: str,
                     hour: int | None = None, sequence_index: int = 0,
                     existing_relationship: bool = False) -> SendResolution:
        """Resolve the mission's parked send-gate. POLICY_AUTO_SEND → auto-approve (policy evidence);
        otherwise deliver the brief and leave the gate pending for the owner's one-tap action."""
        pending = rt.repo.pending_human(mission_id)
        if not pending:
            return SendResolution(SendMode.APPROVE_AND_SEND, False, "pending", "no pending send gate")
        node_id = pending["node_id"]

        mode = policy.mode_for(message_class, opp, hour=hour, sequence_index=sequence_index,
                               existing_relationship=existing_relationship)
        reason = policy.explain(message_class, mode)

        if mode is SendMode.POLICY_AUTO_SEND:
            # pre-authorized: send a courtesy notice, then resolve the gate automatically with evidence.
            brief = build_brief(opp, mission_id, approval_policy=reason)
            self.channel.send("[auto-sent per your policy]\n" + brief.text)
            rt.approve(mission_id, node_id, "approve")
            return SendResolution(mode, True, "policy", reason, brief)

        # owner-in-the-loop: deliver the brief; the owner acts via owner_action(...).
        brief = build_brief(opp, mission_id, approval_policy=reason)
        self.channel.send(brief.text)
        return SendResolution(mode, False, "pending", reason, brief)

    def owner_action(self, rt, mission_id: str, action: str, opp: RevenueOpportunity) -> str:
        """Apply the owner's one-tap decision to the parked send-gate. Returns the resulting state note.
        Every action is a governed Mission event via `rt.approve`."""
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"unknown owner action {action!r}")
        pending = rt.repo.pending_human(mission_id)
        if not pending:
            return "no pending gate"
        node_id = pending["node_id"]

        if action in ("APPROVE_AND_SEND", "MARK_DONE"):
            rt.approve(mission_id, node_id, "approve")
            return "approved — follow-up sent"
        if action in ("REJECT", "CLOSE_LOST"):
            rt.approve(mission_id, node_id, "reject")
            # a rejected send must not lose the lead — leave it owned with a manual next action.
            opp.next_action = "owner declined auto-send — manual follow-up"
            if action == "CLOSE_LOST":
                opp.disposition = Disposition.LOST
                opp.next_action = ""
            return "rejected — lead kept with a manual next action"
        # SNOOZE / ASSIGN / EDIT / CALL / REQUEST_MORE_CONTEXT: leave the gate parked, record intent.
        opp.next_action = f"owner:{action.lower()}"
        return f"deferred — {action}"
