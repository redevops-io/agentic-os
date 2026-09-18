"""Auto-follow-up policy — explicit, explainable authorization for outbound customer contact.

Not a global "AI may send" switch. The policy names the narrow message classes the owner has
pre-authorized for auto-send (inquiry acknowledgements, missed-call replies, appointment
confirmations, …); everything else — custom pricing, discounts, commitments, disputes, government
submissions — stays approval-gated. Every automatic send must be explainable from the policy that
authorized it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import RevenueOpportunity, SendMode

# Message classes that are *eligible* to be pre-authorized. Anything not here is always owner-gated.
AUTO_SENDABLE_CLASSES = frozenset({
    "inquiry_acknowledgement",
    "missed_call_reply",
    "appointment_confirmation",
    "approved_quote_followup",
    "request_missing_routine_info",
    "agreed_action_reminder",
})

# Message classes that may NEVER auto-send, whatever the policy says (safety floor).
NEVER_AUTO = frozenset({
    "custom_pricing", "discount", "contractual_commitment", "dispute",
    "sensitive_financial", "unusual_promise", "gov_bid_submission",
})


@dataclass
class AutoFollowupPolicy:
    """The owner's outbound-contact policy. ``auto_send_classes`` is the subset of AUTO_SENDABLE_CLASSES
    the owner has actually turned on; ``require_existing_relationship`` and ``max_auto_sequence`` bound
    it further."""
    owner: str
    auto_send_classes: frozenset[str] = field(default_factory=frozenset)
    require_existing_relationship: bool = False
    max_auto_sequence: int = 3
    quiet_hours: tuple[int, int] | None = None      # (start_hour, end_hour) in which auto-send is held
    default_mode: SendMode = SendMode.APPROVE_AND_SEND

    def mode_for(self, message_class: str, opp: RevenueOpportunity, *, hour: int | None = None,
                 sequence_index: int = 0, existing_relationship: bool = False) -> SendMode:
        """The authorized send mode for one message on one opportunity. Defaults to owner approval;
        only returns POLICY_AUTO_SEND when the class is pre-authorized AND every guard passes."""
        if message_class in NEVER_AUTO:
            return SendMode.APPROVE_AND_SEND
        if message_class not in self.auto_send_classes:
            return self.default_mode
        if self.require_existing_relationship and not existing_relationship:
            return SendMode.APPROVE_AND_SEND
        if sequence_index >= self.max_auto_sequence:
            return SendMode.APPROVE_AND_SEND
        if self.quiet_hours and hour is not None:
            lo, hi = self.quiet_hours
            in_quiet = (lo <= hour < hi) if lo <= hi else (hour >= lo or hour < hi)
            if in_quiet:
                return SendMode.APPROVE_AND_SEND
        return SendMode.POLICY_AUTO_SEND

    def explain(self, message_class: str, mode: SendMode) -> str:
        """A one-line, auditable reason the send was authorized the way it was."""
        if mode is SendMode.POLICY_AUTO_SEND:
            return (f"policy[{self.owner}]: '{message_class}' is a pre-authorized auto-send class "
                    f"(within sequence/quiet-hour/relationship guards)")
        if message_class in NEVER_AUTO:
            return f"policy[{self.owner}]: '{message_class}' may never auto-send — owner approval required"
        if message_class not in self.auto_send_classes:
            return f"policy[{self.owner}]: '{message_class}' is not a pre-authorized class — owner approval required"
        return f"policy[{self.owner}]: guard not satisfied for '{message_class}' — owner approval required"
