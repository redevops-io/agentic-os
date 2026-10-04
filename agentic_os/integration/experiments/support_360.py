"""Experiment B (§40.B) — support ticket → CRM 360 → Jira → support.

Exercises: identity resolution across systems, parallel context collection, evidence packaging, bidirectional
state, and human-facing output. The flow:

    ticket arrives
      → resolve the requester to ONE canonical customer across support/CRM/billing (conflict → exception)
      → gather CRM + billing context in parallel → an EscalationPackage (the Customer-360 card)
      → create a Jira issue, bound to the ticket (Obligation: issue exists + carries the ticket ref) → receipt
      → mirror the issue ref/status back onto the ticket (Obligation: ticket shows the jira ref) → receipt

Both the Jira create and the write-back are obligations verified by read-back — so a silent failure on either
side is caught, not assumed. No CRM data is replicated into the helpdesk; only decision-relevant context, with
its source.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import EntityType, ExceptionCategory, IntegrationException, Obligation, ResolutionStatus, RetryPolicy
from ..identity import resolve, search_matches
from ..obligations import DischargeResult, ObligationEngine
from ..provider import ActionResult, IntegrationProvider


@dataclass
class EscalationPackage:
    """The Customer-360 context card (plan §22) — decision-relevant context with provenance, not a CRM copy."""
    ticket_id: str
    customer: str
    account: str = ""
    tier: str = ""
    arr: float = 0.0
    billing_status: str = ""
    entitlement: str = ""
    problem: str = ""
    sources: tuple = ()


@dataclass
class Support360Outcome:
    entity_status: ResolutionStatus
    context: "EscalationPackage | None" = None
    jira_discharge: "DischargeResult | None" = None
    mirror_discharge: "DischargeResult | None" = None
    exception: "IntegrationException | None" = None

    @property
    def ok(self) -> bool:
        return (self.jira_discharge is not None and self.jira_discharge.satisfied
                and self.mirror_discharge is not None and self.mirror_discharge.satisfied)


def run_support_360(zendesk: IntegrationProvider, crm: IntegrationProvider, billing: IntegrationProvider,
                    jira: IntegrationProvider, ticket_id: str) -> Support360Outcome:
    ticket = zendesk.read_object("ticket", ticket_id)
    if ticket is None:
        raise ValueError(f"unknown ticket {ticket_id!r}")
    email = ticket.normalized_fields.get("requester_email", "")

    # 1. resolve the requester across CRM + billing by email (identity plane)
    matches = search_matches({"salesforce": crm, "stripe": billing},
                             {"salesforce": "contact", "stripe": "customer"}, "email", email)
    entity = resolve(EntityType.PERSON, "email", email, matches)
    if entity.resolution_status == ResolutionStatus.CONFLICTED:
        exc = IntegrationException(
            category=ExceptionCategory.IDENTITY_CONFLICT, workflow_id="support-360",
            affected_entities=(ticket_id, email), detail="; ".join(entity.evidence[-1:]) or "ambiguous identity",
            business_impact="agent would answer with the wrong account context",
            recommended_action="resolve the duplicate CRM records before auto-attaching context",
            evidence=entity.evidence)
        return Support360Outcome(entity_status=entity.resolution_status, exception=exc)

    # 2. gather decision-relevant context (parallel reads → one evidence package)
    con = next((m.fields for m in matches if m.resource_id == "salesforce"), {})
    cus = next((m.fields for m in matches if m.resource_id == "stripe"), {})
    context = EscalationPackage(
        ticket_id=ticket_id, customer=email, account=con.get("account_name", ""), tier=con.get("tier", ""),
        arr=float(con.get("arr", 0) or 0), billing_status=cus.get("status", ""),
        entitlement="priority" if con.get("tier") == "enterprise" else "standard",
        problem=ticket.normalized_fields.get("subject", ""),
        sources=tuple((b[0], b[1]) for b in entity.source_bindings))

    # 3. create the Jira issue, bound to the ticket — verified by read-back
    issue_id = f"ISSUE-{ticket_id}"

    def _create_issue() -> ActionResult:
        return jira.create_object("issue", {
            "id": issue_id, "summary": context.problem, "ticket_ref": ticket_id, "customer": email,
            "priority": "P1" if context.tier == "enterprise" else "P3", "status": "open",
            "business_impact": f"{context.tier} account, ARR {context.arr:.0f}"}, idempotency_key=issue_id)

    jira_obl = Obligation(
        trigger="zendesk.ticket.escalated", source_resource="zendesk", destination_resource="jira",
        entity_refs=(ticket_id, email), workflow_id="support-360", retry_policy=RetryPolicy(max_attempts=2),
        expected_state={"issue": {"ticket_ref": ticket_id, "customer": email}})
    jira_discharge = ObligationEngine().discharge(
        jira_obl, jira, action=_create_issue, targets={"issue": issue_id}, authority="svc@support")

    if not jira_discharge.satisfied:
        return Support360Outcome(entity_status=entity.resolution_status, context=context,
                                 jira_discharge=jira_discharge, exception=jira_discharge.exception)

    # 4. mirror the Jira ref/status back onto the support ticket — bidirectional, also verified
    def _writeback() -> ActionResult:
        return zendesk.update_object("ticket", ticket_id,
                                     {"jira_ref": issue_id, "status": "escalated"}, idempotency_key=f"wb_{issue_id}")

    mirror_obl = Obligation(
        trigger="jira.issue.created", source_resource="jira", destination_resource="zendesk",
        entity_refs=(ticket_id,), workflow_id="support-360", retry_policy=RetryPolicy(max_attempts=2),
        expected_state={"ticket": {"jira_ref": issue_id, "status": "escalated"}})
    mirror_discharge = ObligationEngine().discharge(
        mirror_obl, zendesk, action=_writeback, targets={"ticket": ticket_id}, authority="svc@support")

    return Support360Outcome(entity_status=entity.resolution_status, context=context,
                             jira_discharge=jira_discharge, mirror_discharge=mirror_discharge,
                             exception=(None if mirror_discharge.satisfied else mirror_discharge.exception))
