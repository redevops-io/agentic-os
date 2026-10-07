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
from .funnel import diagnose_funnel
from .offer import decide_offer


def _offer_handler(*, subject, eligible_offers, constraints, expected_value=None):
    return decide_offer(subject, eligible_offers, constraints=constraints, expected_value=expected_value)


def _campaign_handler(*, hypothesis, **kw):
    return build_campaign_plan(hypothesis, **kw)


def _funnel_handler(*, funnel, stage_conversion, **kw):
    return diagnose_funnel(funnel, stage_conversion=stage_conversion, **kw)


def _deal_state_handler(*, reported, readbacks, reported_source=""):
    return produce_verified_deal(reported, readbacks, reported_source=reported_source)


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
)

_HANDLERS = {
    "acquisition.offer_decide": _offer_handler,
    "acquisition.campaign_plan": _campaign_handler,
    "acquisition.funnel_optimize": _funnel_handler,
    "sales.deal_state": _deal_state_handler,
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
