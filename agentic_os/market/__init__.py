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
    CTA, Experiment, ExperimentOutcome, Funnel, FunnelStep, MarketObservations, MarketPattern, MediaAnalysis,
    MediaArtifact, Offer, Opportunity, PageSnapshot, Provenance, FormObservation, TrackedCompany)
from .adapters import (
    FunnelResolver, MarketSourceAdapter, MarketSourceRegistry, MediaAnalyzer, SimpleFunnelResolver)
from .observe import DEFAULT_PATHS, FetchedPage, HttpPageFetcher, PageFetcher, WebsiteSourceAdapter
from .media import OpenAICompatVisionModel, VisionMediaAnalyzer, VisionModel, analyze_media
from .patterns import detect_patterns
from .opportunities import match_opportunities
from .experiments import ChangeWriter, plan_experiments, proposed
from .execution import DryRunExecutor, ExecutionResult, ExperimentExecutor, ExperimentQueue
from .learning import PatternPrior, learn_priors, reweight_opportunities

__all__ = [
    # evidence contracts
    "TrackedCompany", "PageSnapshot", "MediaArtifact", "MediaAnalysis", "CTA", "Offer", "FormObservation",
    "FunnelStep", "Funnel", "MarketPattern", "Opportunity", "MarketObservations", "Provenance",
    # seams
    "MarketSourceAdapter", "MediaAnalyzer", "FunnelResolver", "MarketSourceRegistry", "SimpleFunnelResolver",
    # website/funnel observation (Phase 1)
    "WebsiteSourceAdapter", "PageFetcher", "HttpPageFetcher", "FetchedPage", "DEFAULT_PATHS",
    # multimodal creative intelligence (Phase 2)
    "VisionMediaAnalyzer", "VisionModel", "OpenAICompatVisionModel", "analyze_media",
    # cross-competitor pattern detection (Phase 3)
    "detect_patterns",
    # pattern -> first-party gap matching (Phase 4)
    "match_opportunities",
    # experiment planning / execution / learning (Phase 5-7)
    "Experiment", "ExperimentOutcome", "plan_experiments", "proposed", "ChangeWriter",
    "ExperimentQueue", "ExperimentExecutor", "DryRunExecutor", "ExecutionResult",
    "learn_priors", "reweight_opportunities", "PatternPrior",
]
