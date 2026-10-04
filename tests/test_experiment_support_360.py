"""Experiment B (§40.B) — support ticket → CRM 360 → Jira → support, on synthetic data.

Proves identity resolution across systems (+ conflict handling), evidence packaging, and BIDIRECTIONAL verified
state (Jira issue created, then mirrored back onto the ticket) — all on the obligation plane, no live systems.
"""
from __future__ import annotations

from agentic_os.integration import ResolutionStatus
from agentic_os.integration.contracts import ExceptionCategory
from agentic_os.integration.experiments.support_360 import run_support_360
from agentic_os.integration.testkit import synthetic_support_case


def test_resolves_identity_builds_context_and_binds_bidirectionally():
    fx = synthetic_support_case(seed=1)
    out = run_support_360(fx.zendesk, fx.crm, fx.billing, fx.jira, fx.ticket_id)
    assert out.entity_status == ResolutionStatus.RESOLVED and out.ok
    # the Customer-360 card carries decision-relevant context with provenance
    assert out.context.account == "ACME Inc" and out.context.tier == "enterprise" and out.context.entitlement == "priority"
    assert len(out.context.sources) == 2                       # resolved across CRM + billing

    # Jira issue was created AND verified (read-back), bound to the ticket
    issue = fx.jira.read_object("issue", f"ISSUE-{fx.ticket_id}")
    assert issue is not None and issue.normalized_fields["ticket_ref"] == fx.ticket_id
    assert issue.normalized_fields["priority"] == "P1"         # enterprise → P1
    assert out.jira_discharge.receipt.observed_state["issue"]["customer"] == fx.email

    # the ticket was mirrored back (bidirectional) and verified
    ticket = fx.zendesk.read_object("ticket", fx.ticket_id)
    assert ticket.normalized_fields["jira_ref"] == f"ISSUE-{fx.ticket_id}"
    assert ticket.normalized_fields["status"] == "escalated"
    assert out.mirror_discharge.satisfied


def test_identity_conflict_blocks_and_raises_not_merges():
    # two CRM contacts share the requester email → CONFLICTED; we must NOT attach wrong context or create Jira
    fx = synthetic_support_case(seed=2, conflict=True)
    out = run_support_360(fx.zendesk, fx.crm, fx.billing, fx.jira, fx.ticket_id)
    assert out.entity_status == ResolutionStatus.CONFLICTED
    assert out.exception and out.exception.category == ExceptionCategory.IDENTITY_CONFLICT
    assert out.context is None
    assert fx.jira.read_object("issue", f"ISSUE-{fx.ticket_id}") is None   # nothing created on ambiguity


def test_silent_jira_failure_is_caught():
    # simulate the Jira write silently not landing → the obligation catches it; no bogus "escalated" write-back
    fx = synthetic_support_case(seed=3)
    fx.jira._drop = True                                        # drop_writes on the jira provider
    out = run_support_360(fx.zendesk, fx.crm, fx.billing, fx.jira, fx.ticket_id)
    assert not out.ok and out.jira_discharge is not None and not out.jira_discharge.satisfied
    assert out.exception.category == ExceptionCategory.OBLIGATION_UNSATISFIED
    assert out.mirror_discharge is None                        # never mirrored a non-existent issue
    assert fx.zendesk.read_object("ticket", fx.ticket_id).normalized_fields.get("status") == "open"


def test_idempotent_rerun_does_not_duplicate_issue():
    fx = synthetic_support_case(seed=4)
    run_support_360(fx.zendesk, fx.crm, fx.billing, fx.jira, fx.ticket_id)
    run_support_360(fx.zendesk, fx.crm, fx.billing, fx.jira, fx.ticket_id)
    assert len(fx.jira._store.get("issue", {})) == 1           # idempotency_key dedupes the issue
