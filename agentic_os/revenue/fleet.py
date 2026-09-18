"""The revenue capability fleet — the app operators a Revenue Mission dispatches across.

These are the GTM-side capabilities of the existing Agentic Apps (CRM/Twenty, Outreach, Lifecycle),
expressed as `CapabilitySpec`s the Mission compiler binds to (by `provides` == the intent step's
outcome). The send capability is `side_effecting` + `approval_required` — the money/contact gate that
parks the mission for the owner. Handlers close over the opportunity so the evidence they emit
(draft text, recipient, channel) is real for this lead.

Offline/in-memory here (the plan's P0 "synthetic end-to-end replay"); the same specs bind to the live
Twenty/Outreach/Listmonk adapters in production without changing the Mission.
"""
from __future__ import annotations

from ..mission.executor import InMemoryOperatorClient
from ..mission.registry import CapabilityRegistry
from ..mission.types import CapabilityManifest, CapabilitySpec
from .contracts import RevenueOpportunity


def revenue_fleet(opp: RevenueOpportunity) -> tuple[CapabilityRegistry, InMemoryOperatorClient]:
    """A capability registry + operator client for one opportunity's Revenue Mission."""
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("agentic-crm", [
        # CRM writes are side-effecting but reversible (compensable) — so they don't trip the dynamic-risk
        # gate; only the customer-contact send below is an owner decision.
        CapabilitySpec("crm.upsert_opportunity", "agentic-crm", provides=["opportunity_recorded"],
                       side_effecting=True, undo="crm.retract_opportunity",
                       permissions=["crm:write"], estimated_value="medium"),
        CapabilitySpec("crm.log_activity", "agentic-crm", provides=["activity_logged"],
                       side_effecting=True, undo="crm.delete_activity",
                       permissions=["crm:write"], estimated_value="low"),
    ]))
    reg.register(CapabilityManifest("outreach-engine", [
        CapabilitySpec("revenue.prepare_response", "outreach-engine", provides=["response_drafted"],
                       permissions=["revenue:draft"], estimated_value="high"),
        # the customer-contact gate: side-effecting + approval-required → parks WAITING_HUMAN
        CapabilitySpec("revenue.send_followup", "outreach-engine", provides=["followup_sent"],
                       side_effecting=True, approval_required=True, undo="revenue.retract_followup",
                       permissions=["revenue:send"], estimated_value="high"),
        CapabilitySpec("revenue.schedule_next_action", "outreach-engine", provides=["next_action_scheduled"],
                       permissions=["revenue:schedule"], estimated_value="medium"),
    ]))

    def _upsert(_inputs):
        return {"crm_record": f"twenty:opp:{opp.opportunity_id}", "stage": "qualified"}

    def _prepare(_inputs):
        draft = opp.proposed_response or (
            f"Hi {opp.contact_name or 'there'}, thanks for reaching out about "
            f"{opp.requested_service or 'your request'} — we can help. …")
        return {"draft": draft, "channel": opp.channel or "email"}

    def _send(inputs):
        # runs only after the approval gate is resolved (owner or policy)
        mode = (inputs or {}).get("authorized_mode", "APPROVE_AND_SEND")
        return {"delivered": True, "recipient": opp.contact_name or opp.company or "customer",
                "channel": opp.channel or "email", "authorized_mode": mode,
                "content": opp.proposed_response or "(drafted follow-up)"}

    def _retract(_inputs):
        return {"retracted": True}

    def _log(_inputs):
        return {"activity": "followup_sent", "crm_record": f"twenty:opp:{opp.opportunity_id}"}

    def _schedule(_inputs):
        return {"next_action": "callback checkpoint (T+2 business days)", "waiting_condition": "awaiting customer reply"}

    handlers = {
        "crm.upsert_opportunity": _upsert,
        "crm.retract_opportunity": lambda _i: {"retracted": True},
        "revenue.prepare_response": _prepare,
        "revenue.send_followup": _send,
        "revenue.retract_followup": _retract,
        "crm.log_activity": _log,
        "crm.delete_activity": lambda _i: {"deleted": True},
        "revenue.schedule_next_action": _schedule,
    }
    return reg, InMemoryOperatorClient(handlers)
