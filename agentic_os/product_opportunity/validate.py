"""Validation missions (Phase 7, plan §16).

A high OpportunityScore is a product HYPOTHESIS, not a roadmap commitment. Before anything is built, the
opportunity becomes a governed validation mission: a FROZEN hypothesis + success-evidence definition + a set of
validation actions chosen for the gap, and it may not run until a human approves it (plan §6/§17 step 11). The
hypothesis and success criteria are content-sealed at plan time so a later result cannot move the goalposts —
the same discipline as the market experiment layer.

This owns the validation PLAN + approval gate; execution rides the existing Mission Runtime (the enterprise
discovery bridge compiles an approved ValidationMission into a sealed mission).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Tuple

from runtime_contracts.protocol import content_hash

from .score import ProductOpportunity

# validation actions (plan §16), cheapest-signal-first
VALIDATION_ACTIONS = ("SEARCH_MORE_INSTANCES", "INSPECT_EXISTING_PRODUCTS", "EXAMINE_WORKAROUNDS",
                      "INTERVIEW_OPERATORS", "LIGHTWEIGHT_DEMO", "WORKFLOW_TEMPLATE_TO_USERS", "LANDING_PAGE",
                      "PILOT", "INSTRUMENT_PARTIAL_CAPABILITY")

_ACTIONS_BY_CLASS = {
    "ALREADY_SUPPORTED": ("INSTRUMENT_PARTIAL_CAPABILITY", "WORKFLOW_TEMPLATE_TO_USERS"),
    "PACKAGE_AS_TEMPLATE": ("WORKFLOW_TEMPLATE_TO_USERS", "LIGHTWEIGHT_DEMO"),
    "NEW_DOMAIN_LOGIC_REQUIRED": ("LIGHTWEIGHT_DEMO", "INTERVIEW_OPERATORS", "LANDING_PAGE"),
    "SMALL_INTEGRATION_GAP": ("SEARCH_MORE_INSTANCES", "LIGHTWEIGHT_DEMO"),
    "NEW_CONNECTOR_REQUIRED": ("SEARCH_MORE_INSTANCES", "INSPECT_EXISTING_PRODUCTS", "INTERVIEW_OPERATORS",
                               "LANDING_PAGE"),
}


@dataclass(frozen=True)
class ValidationMission:
    """A governed validation of one opportunity. ``seal`` content-addresses the FROZEN hypothesis + success
    criteria + the evidence snapshot at plan time; status gates execution behind explicit approval."""
    opportunity_id: str
    title: str
    hypothesis: str
    proposed_actions: Tuple[str, ...]
    success_criteria: Tuple[str, ...]
    evidence_snapshot: dict
    seal: str
    status: str = "DRAFT"                 # DRAFT | APPROVED | RUNNING | VALIDATED | INVALIDATED | REJECTED
    approved_by: str = ""

    @property
    def runnable(self) -> bool:
        return self.status == "APPROVED"


def _target_confirmations(opp: ProductOpportunity) -> int:
    # ask for materially more independent evidence than we already have (min 5), scaled by how cheap it is to test
    return max(5, opp.independent_evidence_count + 3)


def plan_validation(opp: ProductOpportunity) -> ValidationMission:
    """Draft a validation mission for an opportunity — actions chosen for its gap, hypothesis + success frozen."""
    actions = _ACTIONS_BY_CLASS.get(opp.coverage.classification, ("SEARCH_MORE_INSTANCES",))
    if opp.coverage.missing_connectors:
        actions = tuple(dict.fromkeys(actions + ("INSPECT_EXISTING_PRODUCTS",)))
    target = _target_confirmations(opp)
    hypothesis = (f"The workflow pain '{opp.workflow_pain_id}' across {', '.join(opp.applications)} is real, "
                  f"recurring and worth solving for our funnel.")
    success = (
        f"≥{target} independent operators confirm the workflow + its frequency/cost",
        "at least one confirms an existing paid/workaround solution (willingness to pay)",
        {"ALREADY_SUPPORTED": "≥1 existing user configures + repeats the workflow",
         "PACKAGE_AS_TEMPLATE": "a packaged template gets ≥1 configure + launch",
         "NEW_CONNECTOR_REQUIRED": "a landing page / interviews show demand before the connector is built",
         "NEW_DOMAIN_LOGIC_REQUIRED": "a lightweight demo produces a correct decision on real evidence"}
        .get(opp.coverage.classification, "a lightweight test shows a measurable improvement"))
    snapshot = {"independent_evidence": opp.independent_evidence_count, "composite": opp.score.composite,
                "classification": opp.coverage.classification, "applications": list(opp.applications)}
    seal = content_hash({"opportunity_id": opp.opportunity_id, "hypothesis": hypothesis,
                         "actions": list(actions), "success": list(success), "evidence": snapshot})
    return ValidationMission(
        opportunity_id=opp.opportunity_id, title=f"Validate: {opp.title}", hypothesis=hypothesis,
        proposed_actions=actions, success_criteria=success, evidence_snapshot=snapshot, seal=seal, status="DRAFT")


class ValidationError(ValueError):
    pass


def approve(mission: ValidationMission, approver: str) -> ValidationMission:
    """Human approval gate — only a DRAFT may be approved, and an approver is required (plan §6 step 11)."""
    if mission.status != "DRAFT":
        raise ValidationError(f"only a DRAFT may be approved (status={mission.status})")
    if not approver:
        raise ValidationError("an approver is required")
    return replace(mission, status="APPROVED", approved_by=approver)


def reject(mission: ValidationMission, approver: str, reason: str = "") -> ValidationMission:
    return replace(mission, status="REJECTED", approved_by=approver)
