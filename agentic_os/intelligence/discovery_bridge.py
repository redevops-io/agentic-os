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

from .adapters.companies_house import CompaniesHouseProvider
from .adapters.gleif import GleifProvider
from .adapters.open_ownership import OpenOwnershipProvider
from .adapters.opencorporates import OpenCorporatesProvider
from .adapters.openfigi import OpenFigiProvider
from .adapters.opensanctions import OpenSanctionsProvider
from .adapters.sec_edgar import SecEdgarProvider
from .value_store import EvidenceValueStore

# NOTE: the MANAGED paid providers + their credentialed registration (register_managed_providers /
# register_managed_p2) were relocated to the private metered gateway (``intelligence-gateway``) — that is
# the monetization boundary. This open-core bridge ships only the open baseline.


def default_registry(*, opensanctions_key: str = "", opensanctions_base: str = "",
                     opencorporates_token: str = "", openfigi_key: str = "", sec_edgar_ua: str = "",
                     companies_house_key: str = "", open_ownership_token: str = "",
                     open_ownership_base: str = "") -> IntelligenceRegistry:
    """The open baseline registry (Counterparty + Asset identity / ownership). Open, free-to-resell providers:
    GLEIF, SEC EDGAR, OpenFIGI, Open Ownership (always entitled); OpenSanctions (open via yente, or keyed hosted);
    OpenCorporates + UK Companies House (BYO — free keys, entitled only when configured). The private gateway
    registers managed paid providers on top of this via ``managed_registry(open_baseline=True, **kwargs)``."""
    reg = IntelligenceRegistry()
    reg.register(GleifProvider())
    reg.register(SecEdgarProvider(user_agent=sec_edgar_ua))
    reg.register(OpenFigiProvider(api_key=openfigi_key))
    oo_kwargs = {"api_token": open_ownership_token}
    if open_ownership_base:
        oo_kwargs["base_url"] = open_ownership_base
    reg.register(OpenOwnershipProvider(**oo_kwargs))
    os_kwargs = {"api_key": opensanctions_key}
    if opensanctions_base:
        os_kwargs["base_url"] = opensanctions_base
    reg.register(OpenSanctionsProvider(**os_kwargs))
    reg.register(OpenCorporatesProvider(api_token=opencorporates_token))
    reg.register(CompaniesHouseProvider(api_key=companies_house_key))
    return reg


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
