"""Market / Acquisition Intelligence (PUBLIC, Phase 0 — contracts + seams).

An observable-market evidence layer for the Content/Search Intelligence loop: observe how the market acquires
customers, reconstruct the funnels and creative patterns behind those observations, match them to first-party
gaps, and turn the best-supported into GOVERNED experiments whose measured outcomes improve future decisions —

    observe → resolve → detect → hypothesize → propose → approve → execute → measure → learn

This kernel package ships the canonical evidence schema and the read-only observation/analysis/reconstruction
seams. Competitor-specific source lists and credentialed adapters are ReDevOps specifics that live in the
private overlay; the contracts here are the generic capability. Observation is READ-ONLY; consequential
experiments are separately governed by the Mission Runtime, and first-party measured outcomes supersede
competitor imitation as the primary learning signal.
"""
from .contracts import (
    CTA, Funnel, FunnelStep, MarketObservations, MarketPattern, MediaAnalysis, MediaArtifact, Offer,
    Opportunity, PageSnapshot, Provenance, FormObservation, TrackedCompany)
from .adapters import (
    FunnelResolver, MarketSourceAdapter, MarketSourceRegistry, MediaAnalyzer, SimpleFunnelResolver)
from .observe import DEFAULT_PATHS, FetchedPage, HttpPageFetcher, PageFetcher, WebsiteSourceAdapter

__all__ = [
    # evidence contracts
    "TrackedCompany", "PageSnapshot", "MediaArtifact", "MediaAnalysis", "CTA", "Offer", "FormObservation",
    "FunnelStep", "Funnel", "MarketPattern", "Opportunity", "MarketObservations", "Provenance",
    # seams
    "MarketSourceAdapter", "MediaAnalyzer", "FunnelResolver", "MarketSourceRegistry", "SimpleFunnelResolver",
    # website/funnel observation (Phase 1)
    "WebsiteSourceAdapter", "PageFetcher", "HttpPageFetcher", "FetchedPage", "DEFAULT_PATHS",
]
