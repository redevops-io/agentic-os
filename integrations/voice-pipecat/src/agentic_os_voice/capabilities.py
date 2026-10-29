"""The governed capabilities and tool allowlist of the lead-qualification example.

These are ordinary Agent Gateway capabilities, registered in an ordinary ``CapabilityRegistry``. Nothing here
decides who may call them: the gateway's permission check, authority envelope, approval gate and idempotency do.
A deployment replaces :class:`CrmPort` with a real connector and keeps the shape.

Names follow the gateway's ``prepare_* / request_*`` convention: a ``prepare`` capability drafts or looks up and
has no side effect; a ``request`` capability is the governed side effect.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Dict, Optional, Protocol

from agentic_os.agent_gateway import (
    ApprovalPolicy, CapabilityKind, CapabilityManifest, CapabilityRegistry, DataClass, HandlerResult, RiskTier)
from agentic_os.agent_gateway.write_capabilities import CRM_PREPARE_OUTREACH, CRM_REQUEST_OUTREACH_SEND
from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant

from .contracts import ArgSpec, IdentityLevel, ToolSpec

LEAD_ID = r"lead_[a-z0-9]{3,24}"
STATUS_CHOICES = ("qualified", "nurture", "disqualified", "closed_won")      # the MODEL may ask for any; the envelope allows two
BUDGET_CHOICES = ("under_10k", "10k_50k", "over_50k", "unknown")
TIMELINE_CHOICES = ("now", "this_quarter", "later", "unknown")


class CrmPort(Protocol):
    def lead_lookup(self, arguments: dict) -> dict: ...
    def request_lead_update(self, arguments: dict, idempotency_key: str, envelope: object) -> dict: ...
    def prepare_outreach(self, arguments: dict) -> dict: ...
    def send_outreach(self, arguments: dict, idempotency_key: str, envelope: object) -> dict: ...


class HandoffPort(Protocol):
    def request_human(self, arguments: dict, idempotency_key: str, envelope: object) -> dict: ...


LEAD_LOOKUP = CapabilityManifest(
    "crm.lead_lookup", "Look up the CRM record of a lead the caller has been verified for. Read-only.",
    CapabilityKind.DIRECT, permissions=("crm.read",), risk_tier=RiskTier.READ,
    input_schema={"type": "object", "properties": {"lead_id": {"type": "string"}}, "required": ["lead_id"]},
    data_classes=(DataClass.CUSTOMER_CONTENT,))

LEAD_UPDATE = CapabilityManifest(
    "crm.request_lead_update", "Update the qualification fields of a lead. Bounded, reversible; the owner's envelope limits the values.",
    CapabilityKind.DIRECT, permissions=("crm.write",), risk_tier=RiskTier.BOUNDED_WRITE, side_effecting=True,
    idempotent=True, provider="crm", approval_policy=ApprovalPolicy.AUTO,
    input_schema={"type": "object", "properties": {"lead_id": {"type": "string"}, "status": {"type": "string"}}, "required": ["lead_id", "status"]},
    data_classes=(DataClass.CUSTOMER_CONTENT,))

#: the follow-up SEND is registered with a stricter policy than the gateway's default for its tier: outbound text to a
#: person is always a human decision, and an owner's standing grant can never stand in for it.
OUTREACH_PREPARE = CRM_PREPARE_OUTREACH
OUTREACH_SEND = replace(CRM_REQUEST_OUTREACH_SEND, approval_policy=ApprovalPolicy.MANDATORY)

HANDOFF = CapabilityManifest(
    "handoff.request_human", "Ask for a person on the team to contact the caller. Creates a task; promises no time.",
    CapabilityKind.DIRECT, permissions=("handoff.write",), risk_tier=RiskTier.BOUNDED_WRITE, side_effecting=True,
    idempotent=True, provider="handoff", approval_policy=ApprovalPolicy.AUTO)


def _guard(fn) -> HandlerResult:
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001 - a port fault becomes a clean handler error; the gateway keeps the text out of the reply
        from agentic_os.agent_gateway.registry import client_safe_error
        return HandlerResult(ok=False, error=client_safe_error(exc, where="voice capability"))
    ev = (str(out["provider_object_id"]),) if isinstance(out, dict) and out.get("provider_object_id") else ()
    return HandlerResult(ok=True, output=out, evidence_refs=ev)


def build_registry(crm: CrmPort, handoff: Optional[HandoffPort] = None,
                   registry: Optional[CapabilityRegistry] = None) -> CapabilityRegistry:
    reg = registry or CapabilityRegistry()
    reg.register(LEAD_LOOKUP, lambda req, env: _guard(lambda: crm.lead_lookup(dict(req.arguments))))
    reg.register(LEAD_UPDATE, lambda req, env: _guard(lambda: crm.request_lead_update(dict(req.arguments), req.idempotency_key or "", env)))
    reg.register(OUTREACH_PREPARE, lambda req, env: _guard(lambda: crm.prepare_outreach(dict(req.arguments))))
    reg.register(OUTREACH_SEND, lambda req, env: _guard(lambda: crm.send_outreach(dict(req.arguments), req.idempotency_key or "", env)))
    if handoff is not None:
        reg.register(HANDOFF, lambda req, env: _guard(lambda: handoff.request_human(dict(req.arguments), req.idempotency_key or "", env)))
    return reg


def default_tools() -> Dict[str, ToolSpec]:
    """The allowlist the model sees. Anything else is refused before the gateway is asked."""
    lead = ArgSpec("string", pattern=LEAD_ID, max_len=40, description="CRM lead id")
    return {
        "lookup_lead": ToolSpec(
            "lookup_lead", LEAD_LOOKUP.name, {"lead_id": lead}, side_effecting=False, min_identity=IdentityLevel.VERIFIED,
            account_arg="lead_id", description="Look up the lead record."),
        "update_lead": ToolSpec(
            "update_lead", LEAD_UPDATE.name,
            {"lead_id": lead, "status": ArgSpec("enum", choices=STATUS_CHOICES),
             "budget_band": ArgSpec("enum", choices=BUDGET_CHOICES), "timeline": ArgSpec("enum", choices=TIMELINE_CHOICES),
             "need_summary": ArgSpec("string", required=False, max_len=200)},
            side_effecting=True, min_identity=IdentityLevel.VERIFIED, account_arg="lead_id", requires_confirmation=True,
            description="Record the qualification result on the lead."),
        "draft_followup": ToolSpec(
            "draft_followup", OUTREACH_PREPARE.name,
            {"contact": lead, "goal": ArgSpec("string", max_len=200)},
            side_effecting=False, min_identity=IdentityLevel.VERIFIED, account_arg="contact", description="Draft a follow-up message. Sends nothing."),
        "request_followup_send": ToolSpec(
            "request_followup_send", OUTREACH_SEND.name,
            {"contact": lead, "body": ArgSpec("string", max_len=400), "prepared_ref": ArgSpec("string", required=False, max_len=64)},
            side_effecting=True, min_identity=IdentityLevel.VERIFIED, account_arg="contact", requires_confirmation=True,
            intent_args=("contact", "body"), description="Ask for the drafted follow-up to be sent. A person must approve it."),
        "request_human": ToolSpec(
            "request_human", HANDOFF.name,
            {"session_ref": ArgSpec("string", pattern=r"[A-Za-z0-9_.:\-]{1,64}", max_len=64), "category": ArgSpec("string", max_len=32)},
            side_effecting=True, min_identity=IdentityLevel.ANONYMOUS, allowed_while_handoff=True, timeout_s=5.0,
            description="Ask a person to contact the caller."),
    }


def default_grants() -> tuple:
    """The owner's authority for the voice agent, as gateway authority Grants. Narrow on purpose: the model may mark a lead
    qualified or nurture; any other status is outside the grant."""
    return (
        Grant("crm.lead_lookup", resources=(("lead_id", ("lead_*",)),), max_tier=0),
        Grant("crm.request_lead_update", resources=(("lead_id", ("lead_*",)), ("status", ("qualified", "nurture"))), max_tier=1, max_calls=20,
              rate_calls=10, rate_window_s=3600.0),
        Grant("crm.prepare_outreach", max_tier=0),
        Grant("crm.request_outreach_send", max_tier=2, max_calls=5),     # allowed to REACH the approval gate; MANDATORY approval decides
        Grant("handoff.request_human", max_tier=1, max_calls=20),
    )


def default_envelope(tenant: str, *, subject: str = "voice-agent", now: Optional[float] = None, ttl_s: float = 3600.0,
                     grants: Optional[tuple] = None, max_actions: int = 200) -> AuthorityEnvelope:
    """A standing envelope for the voice agent of one tenant. Key: ``tenant:subject`` (the gateway's default)."""
    now = time.time() if now is None else now
    return AuthorityEnvelope(
        tenant=tenant, principal=subject, mission_id=f"{tenant}:{subject}", goal="inbound lead qualification",
        grants=grants if grants is not None else default_grants(), issued_at=now, expires_at=now + ttl_s,
        budget=AutonomyBudget(usd=5.0, max_actions=max_actions, wall_clock_s=ttl_s))
