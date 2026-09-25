"""Discovery → intelligence bridge (moat plan §4.1, §8).

Apps never call a provider directly (no `Twenty → Apollo`). They raise an EvidenceRequest for a CAPABILITY;
Discovery decides via the evidence-value gate whether to acquire, drives the fallback ladder, and records the
lookup for later value accounting. This is the single seam apps/DecisionCases use.
"""
from __future__ import annotations

from typing import Callable, Optional

from runtime_contracts.protocol import (
    AcquisitionResult, EvidenceRequest, EvidenceValueRecord, IntelligenceRegistry, gated_acquire,
)

from .adapters.gleif import GleifProvider
from .adapters.gtm import (
    ApolloProvider, BrandwatchProvider, DnbProvider, SemrushProvider, SimilarwebProvider,
)
from .adapters.opencorporates import OpenCorporatesProvider
from .adapters.opensanctions import OpenSanctionsProvider
from .adapters.payments import StripeRadarProvider
from .adapters.security_ti import CloudflareTiProvider, DefenderTiProvider, VirusTotalProvider
from .value_store import EvidenceValueStore


def default_registry(*, opensanctions_key: str = "", opensanctions_base: str = "",
                     opencorporates_token: str = "") -> IntelligenceRegistry:
    """The open baseline registry: GLEIF (open) + OpenSanctions (open via yente, or keyed hosted) +
    OpenCorporates (BYO token). Paid providers register on top of this as their adapters land."""
    reg = IntelligenceRegistry()
    reg.register(GleifProvider())
    os_kwargs = {"api_key": opensanctions_key}
    if opensanctions_base:
        os_kwargs["base_url"] = opensanctions_base
    reg.register(OpenSanctionsProvider(**os_kwargs))
    reg.register(OpenCorporatesProvider(api_token=opencorporates_token))
    return reg


def register_paid_providers(
    registry: IntelligenceRegistry, *,
    apollo_key: str = "", similarweb_key: str = "", semrush_key: str = "", stripe_key: str = "",
    cloudflare_token: str = "", cloudflare_account: str = "", dnb_token: str = "", brandwatch_token: str = "",
    virustotal_key: str = "", virustotal_commercial: bool = False, defender_token: str = "",
) -> IntelligenceRegistry:
    """Register the paid BYO providers whose credentials the tenant supplied. Anything left blank is simply not
    registered, so the open baseline still works. VirusTotal additionally needs an explicit commercial license."""
    if apollo_key:
        registry.register(ApolloProvider(credential=apollo_key))
    if similarweb_key:
        registry.register(SimilarwebProvider(credential=similarweb_key))
    if semrush_key:
        registry.register(SemrushProvider(credential=semrush_key))
    if stripe_key:
        registry.register(StripeRadarProvider(credential=stripe_key))
    if cloudflare_token and cloudflare_account:
        registry.register(CloudflareTiProvider(credential=cloudflare_token, account_id=cloudflare_account))
    if dnb_token:
        registry.register(DnbProvider(credential=dnb_token))
    if brandwatch_token:
        registry.register(BrandwatchProvider(credential=brandwatch_token))
    if virustotal_key:
        registry.register(VirusTotalProvider(credential=virustotal_key, commercial=virustotal_commercial))
    if defender_token:
        registry.register(DefenderTiProvider(credential=defender_token))
    return registry


def acquire_for_decision(
    registry: IntelligenceRegistry,
    request: EvidenceRequest,
    *,
    value_fn: Callable[[EvidenceRequest], float] = lambda _r: 1.0,
    value_threshold: float = 0.1,
    store: Optional[EvidenceValueStore] = None,
) -> tuple[AcquisitionResult, EvidenceValueRecord, list[str]]:
    """Gate → acquire → record. `value_fn` is Discovery's estimate that the evidence can change the action."""
    result, record, trace = gated_acquire(
        registry, request, value_estimate_fn=value_fn, value_threshold=value_threshold)
    if store is not None:
        store.append(record)
    return result, record, trace
