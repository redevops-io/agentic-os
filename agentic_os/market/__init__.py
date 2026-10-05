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
from .history import (
    ENTITY_TYPES, SURFACE_TYPES, FunnelObservation, MarketChange, MarketEntity, MarketObservation,
    MarketSurface, ObservationHistory, OfferObservation, diff_state,
)
from .resolution import (
    MatchRelationship, MatchThresholds, NormalizedPrice, PriceComponents, PricePosition, ProductAttributes,
    ProductMatch, normalize_price, price_position, resolve_listings, resolve_product,
)
from .marketplace import (
    CompetitorOffer, InMemoryMarketObservationProvider, MarketObservationProvider, PriceRecommendation,
    PricingContext, RelativePosition, recommend_price_response, relative_position,
)
from .strategy import (
    PRIMITIVE_TYPES, SIGNIFICANCE_ORDER, CrossCompetitorStrategy, StrategyPrimitive, SurvivalMetrics,
    classify_change, cross_competitor_strategies, extract_primitives, survival_metrics,
)
from .funnel import FunnelChange, FunnelHistory, diff_funnels

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
    # persistent immutable market model (Market & Funnel Intelligence, Phase 1)
    "MarketEntity", "MarketSurface", "MarketObservation", "OfferObservation", "FunnelObservation",
    "MarketChange", "ObservationHistory", "diff_state", "ENTITY_TYPES", "SURFACE_TYPES",
    # product/offer resolution + unit-economics normalization (Phase 2)
    "ProductAttributes", "ProductMatch", "MatchRelationship", "MatchThresholds", "resolve_product",
    "resolve_listings", "PriceComponents", "NormalizedPrice", "normalize_price", "PricePosition", "price_position",
    # marketplace observation + relative position + recommend-only price response (Phase 3)
    "MarketObservationProvider", "InMemoryMarketObservationProvider", "RelativePosition", "relative_position",
    "PricingContext", "CompetitorOffer", "PriceRecommendation", "recommend_price_response",
    # strategy primitives + survival/weak-signal metrics over history (Phase 4)
    "StrategyPrimitive", "PRIMITIVE_TYPES", "extract_primitives", "classify_change", "SIGNIFICANCE_ORDER",
    "SurvivalMetrics", "survival_metrics", "CrossCompetitorStrategy", "cross_competitor_strategies",
    # funnel versioning + path-change detection (Phase 5)
    "FunnelChange", "FunnelHistory", "diff_funnels",
]
