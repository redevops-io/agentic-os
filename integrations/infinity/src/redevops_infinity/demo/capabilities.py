"""Three governed capabilities of the demo, and the operations that expose them over RAP.

``crm.lead_lookup`` reads. ``crm.request_lead_update`` is a bounded write the owner's envelope allows for two statuses. ``crm.request_outreach_send``
is a consequential write registered with MANDATORY approval: it parks in the approvals inbox until a named person approves, and no RAP message
can approve it."""
from __future__ import annotations

import time
from typing import Optional

from agentic_os.agent_gateway import (
    ApprovalPolicy, CapabilityKind, CapabilityManifest, CapabilityRegistry, DataClass, HandlerResult, RiskTier)
from agentic_os.agent_gateway.registry import client_safe_error
from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant

from ..operation_binding import Arg, OperationSpec

LEAD = r"lead_[a-z0-9]{3,24}"
LOOKUP = CapabilityManifest("crm.lead_lookup", "Look up a lead. Read-only.", CapabilityKind.DIRECT, permissions=("crm.read",),
                            risk_tier=RiskTier.READ, data_classes=(DataClass.CUSTOMER_CONTENT,))
UPDATE = CapabilityManifest("crm.request_lead_update", "Update the status of a lead. Bounded, reversible.", CapabilityKind.DIRECT,
                            permissions=("crm.write",), risk_tier=RiskTier.BOUNDED_WRITE, side_effecting=True, idempotent=True, provider="crm",
                            approval_policy=ApprovalPolicy.AUTO, data_classes=(DataClass.CUSTOMER_CONTENT,))
SEND = CapabilityManifest("crm.request_outreach_send", "Send a follow-up message. A person must approve it.", CapabilityKind.DIRECT,
                          permissions=("crm.write",), risk_tier=RiskTier.CONSEQUENTIAL, side_effecting=True, idempotent=True, provider="crm",
                          approval_policy=ApprovalPolicy.MANDATORY, data_classes=(DataClass.CUSTOMER_CONTENT,))


def _guard(fn) -> HandlerResult:
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        return HandlerResult(ok=False, error=client_safe_error(exc, where="infinity demo capability"))
    ev = (str(out["provider_object_id"]),) if isinstance(out, dict) and out.get("provider_object_id") else ()
    return HandlerResult(ok=True, output=out, evidence_refs=ev)


def build_registry(crm) -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(LOOKUP, lambda req, env: _guard(lambda: crm.lead_lookup(dict(req.arguments))))
    reg.register(UPDATE, lambda req, env: _guard(lambda: crm.request_lead_update(dict(req.arguments), req.idempotency_key or "")))
    reg.register(SEND, lambda req, env: _guard(lambda: crm.request_outreach_send(dict(req.arguments), req.idempotency_key or "")))
    return reg


def default_operations() -> dict:
    lead = Arg("string", pattern=LEAD, max_len=40, description="CRM lead id")
    return {
        "lookup_lead": OperationSpec("lookup_lead", LOOKUP.name, {"lead_id": lead}, description="Look up a lead record.", timeout_s=10, child_allowed=True),
        "update_lead": OperationSpec(
            "update_lead", UPDATE.name,
            {"lead_id": lead, "status": Arg("enum", choices=("qualified", "nurture", "disqualified", "closed_won")),
             "request_ref": Arg("string", required=False, max_len=40, pattern=r"[A-Za-z0-9_.:\-]{1,40}",
                                description="optional client reference; use a new one for a deliberate repeat")},
            description="Record the qualification status of a lead.", side_effecting=True, timeout_s=20),
        "send_followup": OperationSpec(
            "send_followup", SEND.name,
            {"contact": lead, "body": Arg("string", max_len=400), "request_ref": Arg("string", required=False, max_len=40, pattern=r"[A-Za-z0-9_.:\-]{1,40}")},
            description="Ask for a follow-up message to be sent. A person must approve it first.", side_effecting=True, timeout_s=20),
    }


def default_grants() -> tuple:
    """The owner's authority for the runtime's principal. The agent may set two statuses; any other is outside the grant."""
    return (
        Grant("crm.lead_lookup", resources=(("lead_id", ("lead_*",)),), max_tier=0),
        Grant("crm.request_lead_update", resources=(("lead_id", ("lead_*",)), ("status", ("qualified", "nurture"))), max_tier=1, max_calls=50),
        Grant("crm.request_outreach_send", max_tier=2, max_calls=5),       # may REACH the approval gate; MANDATORY approval decides
    )


def default_envelope(tenant: str, principal: str, *, now: Optional[float] = None, ttl_s: float = 3600.0, grants: Optional[tuple] = None,
                     may_delegate: bool = True) -> AuthorityEnvelope:
    now = time.time() if now is None else now
    return AuthorityEnvelope(
        tenant=tenant, principal=principal, mission_id=f"{tenant}:{principal}", goal="infinity runtime actions",
        grants=grants if grants is not None else default_grants(), issued_at=now, expires_at=now + ttl_s, may_delegate=may_delegate,
        budget=AutonomyBudget(usd=5.0, tokens=0, max_actions=200, wall_clock_s=ttl_s, max_children=8, max_depth=2, max_parallel=4))
