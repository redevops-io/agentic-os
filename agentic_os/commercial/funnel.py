"""Funnel & Conversion Intelligence — generalize the own-site funnel (plan §5-§11, P3).

The Phase-0 audit's flagship finding: funnel optimization IS a reusable capability in the kernel, but the only live
instance is single-tenant (own_site config, publisher = PR to redevops-io-web). This module makes the OWN-SITE
operated funnel a first-class, provider-neutral object so any tenant's funnel can be observed, diagnosed, built,
and changed through pluggable providers — not a redevops.io-specific script.

Naming: this is the funnel *we operate* and edit (``ConversionFunnel``), deliberately distinct from
``market.Funnel`` which is a *competitor's reconstructed* acquisition path (observation). The bridge
``intervention_from_opportunity`` turns a ``market.Opportunity`` (the demo.redevops.io/market opportunity queue)
into a funnel intervention candidate — preserving that market→funnel path.

Pure contracts + deterministic diagnosis + the market bridge. Observation/Execution are Protocols the enterprise
overlay binds (Umami/GSC/CMS/Postiz/…); nothing here does I/O or edits a site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple


class SurfaceType(str, Enum):
    AD = "ad"; LANDING = "landing"; ARTICLE = "article"; PRODUCT_PAGE = "product_page"; PRICING = "pricing"
    FORM = "form"; CHECKOUT = "checkout"; EMAIL = "email"; SIGNUP = "signup"; DEMO = "demo"
    FOLLOWUP = "followup"; MARKETPLACE_LISTING = "marketplace_listing"; OTHER = "other"


class FunnelAction(str, Enum):
    """The bounded set of funnel interventions (plan §11). NO_CHANGE is a first-class valid decision (§29)."""
    NO_CHANGE = "no_change"; CHANGE_COPY = "change_copy"; CHANGE_CTA = "change_cta"; CHANGE_OFFER = "change_offer"
    CHANGE_PRICE_PRESENTATION = "change_price_presentation"; CHANGE_PAGE_STRUCTURE = "change_page_structure"
    CHANGE_AUDIENCE = "change_audience"; CHANGE_CHANNEL = "change_channel"; CHANGE_FOLLOW_UP = "change_follow_up"
    ADD_SOCIAL_PROOF = "add_social_proof"; REMOVE_FRICTION = "remove_friction"; CREATE_VARIANT = "create_variant"
    CREATE_NEW_FUNNEL = "create_new_funnel"


@dataclass(frozen=True)
class ConversionStage:
    """One stage of an operated funnel (plan §6). ``editable_capabilities`` says what an execution provider may
    change here."""
    stage_id: str
    name: str = ""
    surface_type: SurfaceType = SurfaceType.OTHER
    resource_ref: str = ""
    entry_condition: str = ""
    success_condition: str = ""
    failure_condition: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    editable_capabilities: Tuple[FunnelAction, ...] = ()


@dataclass(frozen=True)
class ConversionFunnel:
    """A funnel we operate and can change (plan §6). Provider-neutral + tenant-scoped — supports B2C and B2B."""
    funnel_id: str
    tenant_id: str = ""
    objective: str = ""
    audience: str = ""
    entry_surfaces: Tuple[str, ...] = ()
    stages: Tuple[ConversionStage, ...] = ()
    conversion_events: Tuple[str, ...] = ()
    exit_events: Tuple[str, ...] = ()
    value_model: Mapping[str, Any] = field(default_factory=dict)
    channels: Tuple[str, ...] = ()
    providers: Tuple[str, ...] = ()
    policies: Mapping[str, Any] = field(default_factory=dict)
    evidence: Tuple[str, ...] = ()

    def stage(self, stage_id: str) -> Optional[ConversionStage]:
        return next((s for s in self.stages if s.stage_id == stage_id), None)


@dataclass(frozen=True)
class FunnelIntervention:
    """A candidate change to a funnel (approval-gated downstream; nothing here executes) (§10/§11)."""
    funnel_id: str
    action: FunnelAction
    stage_id: str = ""
    hypothesis: str = ""
    expected_value: float = 0.0
    reversibility: str = "reversible"            # reversible | hard_to_reverse
    evidence: Tuple[str, ...] = ()
    confidence: float = 0.0
    source: str = ""                             # e.g. "market.opportunity", "diagnosis"


@dataclass(frozen=True)
class FunnelDiagnosis:
    """Where conversion leaks and the candidate causes — causality is NOT inferred from a drop alone (§9)."""
    funnel_id: str
    affected_stage: str = ""
    affected_segment: str = ""
    observed_change: str = ""
    candidate_causes: Tuple[str, ...] = ()
    contradictory_evidence: Tuple[str, ...] = ()
    confidence: float = 0.0
    economic_impact: float = 0.0
    evidence: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FunnelPlan:
    """A constructed funnel from a commercial objective (§10)."""
    hypothesis: str
    target_segment: str = ""
    stages: Tuple[ConversionStage, ...] = ()
    content: Mapping[str, Any] = field(default_factory=dict)
    offer: str = ""
    channels: Tuple[str, ...] = ()
    follow_up: Tuple[str, ...] = ()
    instrumentation: Tuple[str, ...] = ()
    success_metrics: Tuple[str, ...] = ()
    guardrails: Tuple[str, ...] = ()


class FunnelObservationProvider(Protocol):
    """Provider-neutral funnel evidence (Umami/GSC/CMS/commerce/ads/…). Umami is ONE provider, not the funnel (§7)."""
    def discover_surfaces(self, funnel_id: str) -> Sequence[str]: ...
    def get_events(self, funnel_id: str) -> Sequence[Mapping[str, Any]]: ...
    def get_metrics(self, funnel_id: str) -> Mapping[str, Any]: ...
    def get_segments(self, funnel_id: str) -> Sequence[str]: ...
    def get_attribution(self, funnel_id: str) -> Mapping[str, Any]: ...
    def get_experiments(self, funnel_id: str) -> Sequence[Mapping[str, Any]]: ...


class FunnelExecutionProvider(Protocol):
    """Provider-neutral funnel changes (CMS/Postiz/commerce/CRM/email/ads). The ReDevOps PR-to-web publisher is one
    implementation of this seam (§8). Every change is governed + verifiable."""
    def create_surface(self, funnel_id: str, spec: Mapping[str, Any]) -> str: ...
    def update_surface(self, funnel_id: str, stage_id: str, spec: Mapping[str, Any]) -> str: ...
    def create_variant(self, funnel_id: str, stage_id: str, spec: Mapping[str, Any]) -> str: ...
    def change_offer(self, funnel_id: str, stage_id: str, offer: Mapping[str, Any]) -> str: ...
    def change_content(self, funnel_id: str, stage_id: str, content: Mapping[str, Any]) -> str: ...
    def change_routing(self, funnel_id: str, spec: Mapping[str, Any]) -> str: ...
    def activate(self, change_ref: str) -> str: ...
    def deactivate(self, change_ref: str) -> str: ...
    def verify(self, change_ref: str) -> bool: ...


def diagnose_funnel(funnel: ConversionFunnel, *, stage_conversion: Mapping[str, float],
                    segment: str = "", observed_change: str = "",
                    evidence: Sequence[str] = ()) -> FunnelDiagnosis:
    """Find the leakiest stage deterministically from per-stage conversion rates (0..1). Reports the stage with the
    lowest conversion as ``affected_stage`` and names generic candidate causes — it does NOT assert the cause."""
    rated = [(s.stage_id, float(stage_conversion.get(s.stage_id, 1.0))) for s in funnel.stages]
    if not rated:
        return FunnelDiagnosis(funnel_id=funnel.funnel_id, observed_change=observed_change,
                               evidence=tuple(evidence))
    # lowest conversion wins; ties → earliest stage (stable)
    worst_id, worst_rate = min(rated, key=lambda r: (r[1], [sid for sid, _ in rated].index(r[0])))
    stage = funnel.stage(worst_id)
    causes = ("offer_unclear", "friction_high", "weak_cta", "traffic_quality") if stage else ()
    try:
        baseline = float(funnel.value_model.get("value_per_conversion", 0.0))
    except (TypeError, ValueError):
        baseline = 0.0
    economic_impact = round((1.0 - worst_rate) * baseline, 4)
    return FunnelDiagnosis(funnel_id=funnel.funnel_id, affected_stage=worst_id, affected_segment=segment,
                           observed_change=observed_change, candidate_causes=causes,
                           confidence=round(1.0 - worst_rate, 4), economic_impact=economic_impact,
                           evidence=tuple(evidence))


# gap/pattern prefix → funnel action (preserves the market opportunity → funnel intervention mapping)
_PATTERN_ACTION = {
    "offer_pattern": FunnelAction.CHANGE_OFFER,
    "cta_pattern": FunnelAction.CHANGE_CTA,
    "pricing": FunnelAction.CHANGE_PRICE_PRESENTATION,
    "price": FunnelAction.CHANGE_PRICE_PRESENTATION,
    "social_proof": FunnelAction.ADD_SOCIAL_PROOF,
    "testimonial": FunnelAction.ADD_SOCIAL_PROOF,
    "friction": FunnelAction.REMOVE_FRICTION,
    "copy": FunnelAction.CHANGE_COPY,
}


def action_for_gap(gap: str) -> FunnelAction:
    key = (gap or "").split(":", 1)[0].strip().lower()
    return _PATTERN_ACTION.get(key, FunnelAction.CREATE_VARIANT)


def intervention_from_opportunity(opportunity: Any, *, funnel_id: str = "", stage_id: str = "") -> FunnelIntervention:
    """Bridge a ``market.Opportunity`` (demo.redevops.io/market queue) into a governed funnel intervention
    candidate — preserving the market→funnel proposal path. Duck-typed: reads gap/proposed_experiment/
    expected_value/reversibility/evidence_refs/confidence/site."""
    gap = getattr(opportunity, "gap", "") or getattr(opportunity, "proposed_experiment", "")
    action = action_for_gap(gap)
    return FunnelIntervention(
        funnel_id=funnel_id or getattr(opportunity, "site", ""),
        action=action,
        stage_id=stage_id,
        hypothesis=getattr(opportunity, "proposed_experiment", "") or gap,
        expected_value=float(getattr(opportunity, "expected_value", 0.0) or 0.0),
        reversibility=getattr(opportunity, "reversibility", "reversible"),
        evidence=tuple(getattr(opportunity, "evidence_refs", ()) or ()),
        confidence=float(getattr(opportunity, "confidence", 0.0) or 0.0),
        source="market.opportunity",
    )


__all__ = [
    "SurfaceType", "FunnelAction", "ConversionStage", "ConversionFunnel", "FunnelIntervention",
    "FunnelDiagnosis", "FunnelPlan", "FunnelObservationProvider", "FunnelExecutionProvider",
    "diagnose_funnel", "action_for_gap", "intervention_from_opportunity",
]
