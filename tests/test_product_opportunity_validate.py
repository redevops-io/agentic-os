"""Product Opportunity Intelligence Phase 7 — validation missions.

Proves: an opportunity drafts a validation mission with a frozen hypothesis + success criteria + actions chosen
for its gap; the plan is sealed (re-planning the same opportunity is reproducible); and execution is gated —
only an approved mission is runnable, approval needs an approver, and a non-draft can't be re-approved.
"""
from __future__ import annotations

import pytest

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.product_opportunity import (
    ValidationError, approve, build_opportunity, plan_validation, reject,
)
from agentic_os.product_opportunity.capability_map import CapabilityCoverage
from agentic_os.product_opportunity.cluster import WorkflowCluster
from agentic_os.product_opportunity.contracts import WorkflowPain


def _opp(classification, apps=("stripe", "quickbooks"), ev=4):
    wp = WorkflowPain(prov=Provenance(provider="t"), name="|".join(apps), applications=apps,
                      pain_dimensions=("cross_app", "manual"), business_consequences=("time_cost",),
                      agentic_fit="high")
    cl = WorkflowCluster(workflow_pain=wp, signature="|".join(apps), raw_mentions=ev, independent_evidence_count=ev,
                         independent_authors=ev, sources=("reddit",), communities=("r/x",), applications=apps,
                         first_seen=0, last_seen=1000)
    missing = () if classification != "SMALL_INTEGRATION_GAP" else ("chargebee",)
    return build_opportunity(cl, CapabilityCoverage("wp", apps, missing, (), 1.0, classification))


def test_plan_is_frozen_and_actions_fit_the_gap():
    m = plan_validation(_opp("NEW_CONNECTOR_REQUIRED"))
    assert m.status == "DRAFT" and m.seal and "INTERVIEW_OPERATORS" in m.proposed_actions
    assert any("independent operators confirm" in c for c in m.success_criteria)
    # reproducible seal — same opportunity drafts the same frozen plan
    assert plan_validation(_opp("NEW_CONNECTOR_REQUIRED")).seal == m.seal
    # a different classification picks different actions → different seal
    assert plan_validation(_opp("PACKAGE_AS_TEMPLATE")).seal != m.seal


def test_already_supported_tests_adoption():
    m = plan_validation(_opp("ALREADY_SUPPORTED"))
    assert "INSTRUMENT_PARTIAL_CAPABILITY" in m.proposed_actions or \
           "WORKFLOW_TEMPLATE_TO_USERS" in m.proposed_actions


def test_approval_gate():
    m = plan_validation(_opp("PACKAGE_AS_TEMPLATE"))
    assert not m.runnable                                  # a draft cannot run
    approved = approve(m, "pm@redevops")
    assert approved.runnable and approved.status == "APPROVED" and approved.approved_by == "pm@redevops"
    with pytest.raises(ValidationError):
        approve(m, "")                                     # approver required
    with pytest.raises(ValidationError):
        approve(approved, "someone")                       # already approved → cannot re-approve


def test_reject():
    m = plan_validation(_opp("NEW_CONNECTOR_REQUIRED"))
    r = reject(m, "pm@redevops", "no clear buyer")
    assert r.status == "REJECTED" and not r.runnable
