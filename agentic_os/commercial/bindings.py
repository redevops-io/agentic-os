"""Bind commercial capabilities into the Sidekick registry — make the substrate CALLABLE (P5).

The P1 registry held metadata-only descriptors; the audit's core finding was that rich capabilities had no runtime
callers. This attaches executable handlers to the pure, deterministic capabilities built in P2-P5, so
``registry.get(id).handler(**inputs)`` actually runs them. Heavier capabilities (deal_close, quote_feasibility) need
live state/methodology plumbing bound in the enterprise overlay; the DealState producer that unblocks them is bound
here. Pure wrappers only — governance/approval still flow through the Runtime for anything consequential.
"""
from __future__ import annotations

from typing import Optional

from ..sidekick.capability import CapabilityDomain as D, CapabilityRegistry, Maturity as M, SidekickCapability, default_registry
from .campaign import build_campaign_plan
from .deal_state import produce_verified_deal
from .forecast import forecast_pipeline
from .funnel import diagnose_funnel
from .offer import decide_offer
from .order import evaluate_order
from .receivables import assess_receivable
from .segment import segment_accounts
from .service import plan_service


def _offer_handler(*, subject, eligible_offers, constraints, expected_value=None):
    return decide_offer(subject, eligible_offers, constraints=constraints, expected_value=expected_value)


def _campaign_handler(*, hypothesis, **kw):
    return build_campaign_plan(hypothesis, **kw)


def _funnel_handler(*, funnel, stage_conversion, **kw):
    return diagnose_funnel(funnel, stage_conversion=stage_conversion, **kw)


def _deal_state_handler(*, reported, readbacks, reported_source=""):
    return produce_verified_deal(reported, readbacks, reported_source=reported_source)


def _order_execute_handler(*, order, observed_stage_states, now, stage_deadlines=None):
    """Detect missed/overdue/diverged order transitions (read-only intelligence). Returns the incident tuple;
    remediation Missions are proposed as candidate_actions and stay authority-gated through the Runtime."""
    return evaluate_order(order, observed_stage_states=observed_stage_states, now=now, stage_deadlines=stage_deadlines)


def _service_plan_handler(*, customer_commitment, requested_service, requirements, resources,
                          required_by=0, location="", sla=""):
    return plan_service(customer_commitment, requested_service, requirements, resources,
                        required_by=required_by, location=location, sla=sla)


def _receivables_handler(*, case):
    return assess_receivable(case)


def _forecast_handler(*, deals, now=None, unsupported_probability_cap=0.5):
    return forecast_pipeline(deals, now=now, unsupported_probability_cap=unsupported_probability_cap)


def _segment_handler(*, accounts, signals=None, confidence_floor=0.5):
    return segment_accounts(accounts, signals=signals, confidence_floor=confidence_floor)


# descriptors for the P4 capabilities not in the seed catalog
_NEW_DESCRIPTORS = (
    SidekickCapability(
        capability_id="acquisition.offer_decide", domain=D.ACQUISITION,
        summary="Choose the best eligible offer under policy (margin/kind/capacity/discount), or NO_ACTION.",
        intents=("which offer", "pick an offer", "what should we offer", "best offer for"),
        required_inputs=("subject", "eligible_offers", "constraints"),
        produced_artifacts=("OfferDecision",), candidate_actions=("select_offer", "NO_ACTION"),
        authority_requirements=("pricing_policy",), verification_contract="hard-constraint feasibility",
        maturity=M.L2_GENERALIZED, status="COMPLETE", code_ref="agentic_os/commercial/offer.py"),
    SidekickCapability(
        capability_id="acquisition.campaign_plan", domain=D.ACQUISITION,
        summary="Compose a campaign plan (audience × offer × message × channel) from a commercial hypothesis.",
        intents=("plan a campaign", "build a campaign", "launch a campaign for"),
        required_inputs=("hypothesis",), produced_artifacts=("CampaignPlan",),
        candidate_actions=("draft_campaign", "request_approval"), authority_requirements=("brand_policy",),
        maturity=M.L2_GENERALIZED, status="COMPLETE", code_ref="agentic_os/commercial/campaign.py"),
    SidekickCapability(
        capability_id="operations.order_execute", domain=D.OPERATIONS,
        summary="Monitor an order across its fulfillment chain (received→…→verified) and detect missed/overdue/"
                "diverged transitions, proposing authority-gated remediation.",
        intents=("is this order on track", "why is this order stuck", "track this order",
                 "order fulfillment status", "order execution status", "is this order delayed"),
        required_inputs=("order", "observed_stage_states", "now"), optional_inputs=("stage_deadlines",),
        produced_artifacts=("ReliabilityIncident",),
        candidate_actions=("remediate_transition", "NO_ACTION"),
        verification_contract="generic expected-transition monitor; remediation stays authority-gated",
        maturity=M.L2_GENERALIZED, status="COMPLETE", code_ref="agentic_os/commercial/order.py"),
    SidekickCapability(
        capability_id="operations.service_plan", domain=D.OPERATIONS,
        summary="Plan a delivered service and check feasibility (skills/capacity vs deadline), returning the "
                "assignment and resourcing risks.",
        intents=("can we deliver this service", "plan this service", "service feasibility",
                 "do we have capacity for", "can we staff this job", "schedule a service"),
        required_inputs=("customer_commitment", "requested_service", "requirements", "resources"),
        optional_inputs=("required_by", "location", "sla"),
        produced_artifacts=("ServicePlan", "ServiceFeasibility"),
        candidate_actions=("schedule_service", "resolve_resourcing", "NO_ACTION"),
        verification_contract="deterministic greedy skill-capacity match over resolved facts",
        maturity=M.L2_GENERALIZED, status="COMPLETE", code_ref="agentic_os/commercial/service.py"),
    SidekickCapability(
        capability_id="commercial.forecast", domain=D.COMMERCIAL,
        summary="Weighted sales-pipeline forecast that separates CRM-reported optimism from the runtime-VERIFIED "
                "committed total, surfacing the at-risk gap.",
        intents=("forecast the pipeline", "what's the forecast", "revenue forecast", "weighted pipeline",
                 "what will we close", "pipeline forecast"),
        required_inputs=("deals",), optional_inputs=("now", "unsupported_probability_cap"),
        produced_artifacts=("PipelineForecast",), candidate_actions=("verify_deal", "NO_ACTION"),
        verification_contract="committed EV caps unverified high probabilities; conflicted deals contribute 0",
        maturity=M.L2_GENERALIZED, status="COMPLETE", code_ref="agentic_os/commercial/forecast.py"),
    SidekickCapability(
        capability_id="commercial.segment", domain=D.COMMERCIAL,
        summary="Segment accounts into value tiers from resolved identity + value/health signals; never high-tiers "
                "an unresolved identity.",
        intents=("segment our accounts", "which accounts are strategic", "account segmentation",
                 "tier our customers", "segment customers"),
        required_inputs=("accounts",), optional_inputs=("signals", "confidence_floor"),
        produced_artifacts=("SegmentationResult",), candidate_actions=("assign_segment", "NO_ACTION"),
        verification_contract="unresolved/low-confidence identities are flagged UNVERIFIED, never high-tiered",
        maturity=M.L2_GENERALIZED, status="COMPLETE", code_ref="agentic_os/commercial/segment.py"),
)

_HANDLERS = {
    "acquisition.offer_decide": _offer_handler,
    "acquisition.campaign_plan": _campaign_handler,
    "acquisition.funnel_optimize": _funnel_handler,
    "sales.deal_state": _deal_state_handler,
    "operations.order_execute": _order_execute_handler,
    "operations.service_plan": _service_plan_handler,
    "finance.receivables": _receivables_handler,
    "commercial.forecast": _forecast_handler,
    "commercial.segment": _segment_handler,
}


def bind_commercial_capabilities(registry: Optional[CapabilityRegistry] = None) -> tuple:
    """Register the P4 descriptors (if absent) and bind handlers onto the callable capabilities. Returns the bound
    capability ids. Idempotent."""
    reg = registry or default_registry
    for cap in _NEW_DESCRIPTORS:
        if reg.get(cap.capability_id) is None:
            reg.register(cap)
    bound = []
    for cid, fn in _HANDLERS.items():
        if reg.get(cid) is not None:
            reg.bind(cid, fn)
            bound.append(cid)
    return tuple(sorted(bound))


__all__ = ["bind_commercial_capabilities"]
