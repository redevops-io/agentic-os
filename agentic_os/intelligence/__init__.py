"""Agentic Apps external/professional intelligence layer (moat plan WP2+).

Adapters implement the runtime_contracts intelligence provider contract; the Discovery bridge is the single seam
apps use to acquire external evidence for a decision (gate → acquire → value-account). The open stack ships the
open adapters (GLEIF/OpenSanctions/OpenCorporates); paid providers register on top as Bring-Your-Own.
"""
from .adapters.gleif import GleifProvider
from .adapters.opencorporates import OpenCorporatesProvider
from .adapters.opensanctions import OpenSanctionsProvider
from .adapters.gtm import (
    ApolloProvider, BrandwatchProvider, DnbProvider, SemrushProvider, SimilarwebProvider,
)
from .adapters.payments import StripeRadarProvider
from .adapters.security_ti import CloudflareTiProvider, DefenderTiProvider, VirusTotalProvider
from .apps import AppProfile, app_capabilities, app_profile, request_for
from .discovery_bridge import acquire_for_decision, default_registry, register_paid_providers
from .evaluation import ProviderEvaluation, evaluate, report
from .imports import (
    HistoricalOutcome, from_intercom, from_klaviyo, from_zendesk, seed_value_store,
)
from .value_store import EvidenceValueStore

__all__ = [
    # open providers
    "GleifProvider",
    "OpenSanctionsProvider",
    "OpenCorporatesProvider",
    # paid BYO providers
    "ApolloProvider",
    "SimilarwebProvider",
    "SemrushProvider",
    "DnbProvider",
    "BrandwatchProvider",
    "StripeRadarProvider",
    "CloudflareTiProvider",
    "VirusTotalProvider",
    "DefenderTiProvider",
    # registry + bridge + accounting
    "default_registry",
    "register_paid_providers",
    "acquire_for_decision",
    "EvidenceValueStore",
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
