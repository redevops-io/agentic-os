"""Agentic Apps external/professional intelligence layer (moat plan WP2+).

Adapters implement the runtime_contracts intelligence provider contract; the Discovery bridge is the single seam
apps use to acquire external evidence for a decision (gate → acquire → value-account). This open-core layer ships
the OPEN adapters (GLEIF/OpenSanctions/OpenCorporates) and the own-data families. The MANAGED paid providers,
their credentialed wiring, and provider-value / paid-spend routing live in the private metered gateway
(``intelligence-gateway``), which composes this framework — that is the monetization boundary.
"""
from .adapters.gleif import GleifProvider
from .adapters.opencorporates import OpenCorporatesProvider
from .adapters.opensanctions import OpenSanctionsProvider
from .apps import AppProfile, app_capabilities, app_profile, request_for
from .discovery_bridge import acquire_for_decision, default_registry
from .decision_resolver import Synthesis, Synthesizer, default_synthesize, resolve_decision_need
from .evaluation import ProviderEvaluation, evaluate, report
from .imports import (
    HistoricalOutcome, from_intercom, from_klaviyo, from_zendesk, seed_value_store,
)
from .value_store import EvidenceValueStore
from .service import IntelligenceService, Quote
from .api import build_router
from .temporal_graph import (
    GraphEntity,
    GraphRelationship,
    GraphSnapshot,
    LifecycleEvent,
    TemporalGraph,
    project_kyc_ownership,
    screen_ownership,
)

__all__ = [
    # open providers (open-core; managed paid providers live in the private intelligence-gateway)
    "GleifProvider",
    "OpenSanctionsProvider",
    "OpenCorporatesProvider",
    # registry + bridge + accounting
    "default_registry",
    "acquire_for_decision",
    "resolve_decision_need",
    "default_synthesize",
    "Synthesis",
    "Synthesizer",
    "EvidenceValueStore",
    "IntelligenceService",
    "Quote",
    "build_router",
    "TemporalGraph",
    "GraphEntity",
    "GraphRelationship",
    "GraphSnapshot",
    "LifecycleEvent",
    "project_kyc_ownership",
    "screen_ownership",
    # per-app capability wiring (§4.1, §7)
    "AppProfile",
    "app_profile",
    "app_capabilities",
    "request_for",
    # historical outcome migration (WP7)
    "HistoricalOutcome",
    "from_zendesk",
    "from_intercom",
    "from_klaviyo",
    "seed_value_store",
    # provider evaluation harness (§9)
    "ProviderEvaluation",
    "evaluate",
    "report",
]
