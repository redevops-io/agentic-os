"""Campaign & Content Intelligence — commercial hypothesis first, content second (plan §12, P4).

Generalizes Content/Search + Market/Funnel work: market evidence + audience + product + CRM/customer state + prior
outcomes form a ``CampaignHypothesis``; a ``CampaignPlan`` (audience × offer × message × channel) is composed FROM
that hypothesis, with instrumentation and a measurement plan. Content generation is subordinate to the commercial
hypothesis — never the other way round. Pure contracts + a composer; publishing/creative run through the content
providers (Postiz/Listmonk/site) in the enterprise overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class CampaignHypothesis:
    """Why this campaign should work, grounded in evidence (§12)."""
    objective: str
    audience: str
    product: str = ""
    message: str = ""
    channels: Tuple[str, ...] = ()
    rationale: str = ""
    evidence: Tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class CampaignPlan:
    """An executable campaign plan (§12). Approval + brand policy gate anything consequential; measurement is
    declared up front so the outcome can feed learning."""
    objective: str
    audience: str
    offer: str = ""
    message: str = ""
    channels: Tuple[str, ...] = ()
    assets: Tuple[str, ...] = ()
    schedule: Tuple[str, ...] = ()
    conversion_target: str = ""
    budget_constraints: Mapping[str, Any] = field(default_factory=dict)
    brand_policy: str = ""
    approval_policy: str = ""
    measurement_plan: Tuple[str, ...] = ()
    hypothesis: Optional[CampaignHypothesis] = None

    @property
    def requires_approval(self) -> bool:
        return self.approval_policy not in ("", "auto")


def build_campaign_plan(hypothesis: CampaignHypothesis, *, offer: str = "", assets: Sequence[str] = (),
                        schedule: Sequence[str] = (), conversion_target: str = "",
                        budget_constraints: Optional[Mapping[str, Any]] = None, brand_policy: str = "",
                        approval_policy: str = "approval_required",
                        measurement_plan: Sequence[str] = ()) -> CampaignPlan:
    """Compose a plan subordinate to the hypothesis: objective/audience/message/channels come from the hypothesis;
    the caller supplies offer/assets/schedule/measurement. Defaults to requiring approval before anything runs."""
    measurement = tuple(measurement_plan) or (f"conversion:{conversion_target or hypothesis.objective}",)
    return CampaignPlan(
        objective=hypothesis.objective, audience=hypothesis.audience, offer=offer,
        message=hypothesis.message, channels=hypothesis.channels, assets=tuple(assets),
        schedule=tuple(schedule), conversion_target=conversion_target or hypothesis.objective,
        budget_constraints=dict(budget_constraints or {}), brand_policy=brand_policy,
        approval_policy=approval_policy, measurement_plan=measurement, hypothesis=hypothesis,
    )


__all__ = ["CampaignHypothesis", "CampaignPlan", "build_campaign_plan"]
