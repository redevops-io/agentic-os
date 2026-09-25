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
from .adapters.opencorporates import OpenCorporatesProvider
from .adapters.opensanctions import OpenSanctionsProvider
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
