"""Experiment C (§40.C) — Slack approval → ERP/billing action.

Exercises: chat UX, authority, durable approval, credentialed action, destination verification. Slack/Teams are
surfaces; the approval is a Runtime object. The flow:

    consequential action proposed (e.g. issue a credit note)
      → post an approval prompt to the channel (CollaborationProvider.open_interaction)
      → record a durable ApprovalRequest (pending)
      → decision arrives (approve/reject)   [the Slack button callback, injected here as `decide`]
      → approved → execute the ERP action under an Obligation, verified by read-back → receipt
      → rejected → NOTHING executes; the request is closed as rejected

A 200 from the ERP write does not end it — the obligation is satisfied only when the ledger/billing object is
read back. The approval surface is swappable (Slack↔Teams↔WhatsApp) because it binds to the common
CollaborationProvider contract, not a vendor method.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agentic_os.collaboration import OutboundMessage

from ..contracts import ExceptionCategory, IntegrationException, Obligation, RetryPolicy, _new_id
from ..obligations import DischargeResult, ObligationEngine
from ..provider import ActionResult, IntegrationProvider


@dataclass
class ApprovalRequest:
    action: str
    target_resource: str
    evidence_summary: str
    authority_required: str
    approvers: tuple = ()
    channel: str = ""
    message_id: str = ""
    status: str = "pending"            # pending | approved | rejected | expired
    decided_by: str = ""
    approval_id: str = field(default_factory=lambda: _new_id("appr"))


@dataclass
class ActionSpec:
    """The consequential ERP/billing write gated by the approval."""
    object_type: str
    external_id: str
    fields: dict
    verify_fields: dict                # the subset the obligation must observe on read-back


@dataclass
class SlackApprovalOutcome:
    approval: ApprovalRequest
    posted: bool
    discharge: "DischargeResult | None" = None
    exception: "IntegrationException | None" = None

    @property
    def executed(self) -> bool:
        return self.discharge is not None and self.discharge.satisfied


# decide(approval) -> "approve" | "reject"  — the Slack button callback (injected in tests / wired to inbound events live)
Decider = Callable[[ApprovalRequest], str]


def run_slack_approval(collab: Any, erp: IntegrationProvider, action_spec: ActionSpec, *, channel: str,
                       decide: Decider, authority_required: str = "finance.approver",
                       approvers: tuple = ()) -> SlackApprovalOutcome:
    # 1. post the approval prompt to the channel (any CollaborationProvider: Slack/Teams/WhatsApp)
    prompt = (f"Approve: *{action_spec.object_type}* on {erp.provider} "
              f"({', '.join(f'{k}={v}' for k, v in action_spec.verify_fields.items())})?")
    interaction = collab.open_interaction(channel, prompt, actions=("Approve", "Reject"))
    appr = ApprovalRequest(
        action=f"{action_spec.object_type}.write", target_resource=erp.provider,
        evidence_summary=prompt, authority_required=authority_required, approvers=approvers,
        channel=channel, message_id=getattr(interaction, "message_id", ""),
        status="pending")
    posted = bool(getattr(interaction, "ok", False))

    # 2. the decision (durable approval object records who/what)
    decision = decide(appr)
    if decision != "approve":
        appr.status = "rejected"
        try:
            collab.post_message(OutboundMessage(channel=channel, text=f"❌ Rejected: {appr.action}"))
        except Exception:  # noqa: BLE001
            pass
        return SlackApprovalOutcome(approval=appr, posted=posted)

    appr.status = "approved"

    # 3. approved → execute the credentialed ERP action under an Obligation, verified by read-back
    def _act() -> ActionResult:
        existing = erp.read_object(action_spec.object_type, action_spec.external_id)
        if existing is None:
            return erp.create_object(action_spec.object_type, {"id": action_spec.external_id, **action_spec.fields},
                                     idempotency_key=action_spec.external_id)
        return erp.update_object(action_spec.object_type, action_spec.external_id, action_spec.fields,
                                 idempotency_key=action_spec.external_id)

    obligation = Obligation(
        trigger="slack.approval.granted", source_resource="slack", destination_resource=erp.provider,
        workflow_id="chat-approval", retry_policy=RetryPolicy(max_attempts=2),
        verification_policy="read_after_write",
        expected_state={action_spec.object_type: dict(action_spec.verify_fields)})
    discharge = ObligationEngine().discharge(
        obligation, erp, action=_act, targets={action_spec.object_type: action_spec.external_id},
        authority=appr.authority_required)

    try:
        ok = discharge.satisfied
        collab.post_message(OutboundMessage(channel=channel,
                            text=(f"✅ Done: {appr.action}" if ok else f"⚠ Approved but not verified: {appr.action}")))
    except Exception:  # noqa: BLE001
        pass
    return SlackApprovalOutcome(approval=appr, posted=posted, discharge=discharge,
                                exception=(None if discharge.satisfied else discharge.exception))
