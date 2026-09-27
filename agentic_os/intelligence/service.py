"""Unified intelligence service — the one entry point behind the plan's §3 API.

`POST /v1/intelligence/{domain}/{capability}` → `IntelligenceService.resolve`,
`POST /v1/intelligence/quote`                 → `IntelligenceService.quote`,
`GET  /v1/intelligence/requests/{id}`         → `IntelligenceService.get`.

A caller binds each family's registry + synthesizer (supplier / order / supply / process / counterparty /
asset), and the service routes a DecisionNeed to the right one by its capability, resolves it through the
same governed `resolve_decision_need` path, and stores the result by its fingerprint so it can be fetched
and replayed. Quote estimates the likely provider calls, price ceiling, disclosed fields and latency
*before* any spend — honouring "internal evidence first, pre-call quote, spend caps".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from runtime_contracts.protocol import (
    Capability, DecisionNeed, HealthStatus, IntelligenceRegistry, IntelligenceResult, provider_health,
)

from .decision_resolver import Synthesizer, default_synthesize, resolve_decision_need
from .value_store import EvidenceValueStore


@dataclass(frozen=True)
class Quote:
    capability: str
    available: bool
    providers: tuple[str, ...]          # entitled, permitted, healthy providers, cheapest first
    expected_cost: float                # the cheapest provider's estimate (the ladder stops at first success)
    price_ceiling: float                # worst case: every provider tried
    max_latency_ms: float
    fields_disclosed: tuple[str, ...]   # fields that would be sent to a provider (prohibited ones removed)
    reason: str = ""


@dataclass
class _Binding:
    registry: IntelligenceRegistry
    synthesize: Synthesizer


@dataclass
class IntelligenceService:
    """Routes a DecisionNeed to the family that serves its capability. Bind each family registry once."""
    store: Optional[EvidenceValueStore] = None
    _bindings: dict[Capability, _Binding] = field(default_factory=dict)
    _results: dict[str, IntelligenceResult] = field(default_factory=dict)

    def bind(self, registry: IntelligenceRegistry, synthesize: Synthesizer = default_synthesize) -> "IntelligenceService":
        """Register a family: every capability its providers serve routes to this registry + synthesizer."""
        for p in registry.all():
            for cap in p.capabilities():
                self._bindings[cap] = _Binding(registry, synthesize)
        return self

    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(c.value for c in self._bindings))

    # POST /v1/intelligence/quote
    def quote(self, need: DecisionNeed) -> Quote:
        b = self._bindings.get(need.capability)
        if b is None:
            return Quote(need.capability.value, False, (), 0.0, 0.0, 0.0, (), "capability not served")
        request = need.to_evidence_request()
        cands = [p for p in b.registry.match(request)
                 if need.permits_provider(p.provider_id)
                 and provider_health(p).status is not HealthStatus.UNAVAILABLE]
        if not cands:
            return Quote(need.capability.value, False, (), 0.0, 0.0, 0.0, tuple(request.fields),
                         "no permitted, healthy, entitled provider")
        costs = [p.estimate_cost(request) for p in cands]
        return Quote(
            capability=need.capability.value, available=True,
            providers=tuple(p.provider_id for p in cands),
            expected_cost=round(costs[0].money, 6),               # cheapest-first → first is expected
            price_ceiling=round(sum(c.money for c in costs), 6),  # worst case: whole ladder tried
            max_latency_ms=max((c.latency_ms for c in costs), default=0.0),
            fields_disclosed=tuple(request.fields))

    # POST /v1/intelligence/{domain}/{capability}
    def resolve(self, need: DecisionNeed, **kw) -> IntelligenceResult:
        b = self._bindings.get(need.capability)
        if b is None:
            res = IntelligenceResult.from_artifacts(need, (), (), answer="", confidence=0.0,
                                                    unresolved_gaps=("capability not served",))
            self._results[res.fingerprint()] = res
            return res
        res, _trace = resolve_decision_need(b.registry, need, synthesize=b.synthesize, store=self.store, **kw)
        self._results[res.fingerprint()] = res
        return res

    # GET /v1/intelligence/requests/{id}
    def get(self, request_id: str) -> Optional[IntelligenceResult]:
        return self._results.get(request_id)
