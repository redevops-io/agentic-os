"""Serve the Supplier reliability metrics through the broker as an INTERNAL provider.

The plan's rule is "internal evidence first": before any paid provider, the runtime answers from the
tenant's own canonical events. So supplier reliability is exposed as a cost-0 `IntelligenceProvider`
(family INTERNAL_COMPUTED) — a `DecisionNeed` for DELIVERY_RELIABILITY / CONFIRMATION_RELIABILITY resolves
to it through the same `resolve_decision_need` path (receipt ledger, health, value accounting) as any
external provider, and a supplier synthesizer turns the metric into a business answer.

Leakage-safe replay is available by scoping the event source (`InMemorySupplyEvents(..., as_of_ms=T)`),
so a resolved answer reconstructs what was knowable at a past decision time.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

from ...integrations.business.supply import GoodsReceipt, PurchaseOrder, SupplierCommitment
from .supplier import confirmation_reliability, delivery_reliability

_SERVES = (Capability.DELIVERY_RELIABILITY, Capability.CONFIRMATION_RELIABILITY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── event source ────────────────────────────────────────────────────────────────────────────────────────
class SupplyEventSource(Protocol):
    """The tenant's canonical supply events, by supplier. A store-backed source can implement this; the
    in-memory one below is enough for first deployments and tests."""
    def orders(self, supplier_ref: str) -> Iterable[PurchaseOrder]: ...
    def receipts(self, supplier_ref: str) -> Iterable[GoodsReceipt]: ...
    def commitments(self, supplier_ref: str) -> Iterable[SupplierCommitment]: ...


@dataclass
class InMemorySupplyEvents:
    """In-memory supply events. `as_of_ms` (optional) pre-filters to what was knowable by then, so a
    replay of a past decision never sees a later event."""
    _orders: list[PurchaseOrder] = field(default_factory=list)
    _receipts: list[GoodsReceipt] = field(default_factory=list)
    _commitments: list[SupplierCommitment] = field(default_factory=list)
    as_of_ms: int = 0

    def _knowable(self, o) -> bool:
        if not self.as_of_ms:
            return True
        return (o.prov.known_at or o.prov.observed_at) <= self.as_of_ms

    def orders(self, supplier_ref: str):
        return [o for o in self._orders if o.supplier_ref == supplier_ref and self._knowable(o)]

    def receipts(self, supplier_ref: str):
        return [r for r in self._receipts if r.supplier_ref == supplier_ref and self._knowable(r)]

    def commitments(self, supplier_ref: str):
        return [c for c in self._commitments if c.supplier_ref == supplier_ref and self._knowable(c)]


# ── internal provider ───────────────────────────────────────────────────────────────────────────────────
@dataclass
class SupplierMetricsProvider:
    """Computes supplier reliability from the tenant's own events — cost 0, always entitled (own data)."""
    source: SupplyEventSource
    provider_id: str = "internal.supplier_metrics"
    family: ProviderFamily = ProviderFamily.INTERNAL_COMPUTED

    def capabilities(self) -> tuple[Capability, ...]:
        return _SERVES

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=1)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in _SERVES        # the tenant's own data — always entitled

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        supplier = request.subject_refs[0] if request.subject_refs else ""
        site = request.subject_refs[1] if len(request.subject_refs) > 1 else ""
        if request.capability is Capability.DELIVERY_RELIABILITY:
            m = delivery_reliability(self.source.orders(supplier), self.source.receipts(supplier),
                                     supplier_ref=supplier, site=site)
        elif request.capability is Capability.CONFIRMATION_RELIABILITY:
            m = confirmation_reliability(self.source.orders(supplier), self.source.commitments(supplier),
                                         supplier_ref=supplier)
        else:
            return AcquisitionResult.failed(AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE,
                                            f"{request.capability.value} not served")
        if m.n == 0:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no events for supplier")
        payload = asdict(m)
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=request.capability, subject=supplier,
            observations=(payload,), retrieved_at=_now_iso(), freshness_s=0.0, confidence=m.confidence,
            cost=0.0, license_scope="internal", raw_response_digest=content_hash(payload))
        return AcquisitionResult.found((art,), cost=0.0)


def supplier_registry(source: SupplyEventSource) -> IntelligenceRegistry:
    """A registry with just the internal supplier-metrics provider — the base every supplier DecisionNeed
    resolves against (external firmographic providers register on top for SUPPLIER_RESOLUTION later)."""
    reg = IntelligenceRegistry()
    reg.register(SupplierMetricsProvider(source))
    return reg


# ── family synthesizer (turns the metric artifact into a business answer) ─────────────────────────────────
def supplier_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    """The Supplier family's answer synthesis (plugged into resolve_decision_need). Reads the metric the
    internal provider produced and states it plainly; flags a thin sample and a sub-threshold confidence."""
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    confidence = a.confidence
    cap = need.capability
    if cap is Capability.DELIVERY_RELIABILITY:
        answer = (f"{m['supplier_ref']}: OTIF {m['otif']:.0%} over {m['n']} orders "
                  f"(on-time {m['on_time_rate']:.0%}, in-full {m['in_full_rate']:.0%}); "
                  f"p90 {m['p90_days_late']:g}d late.")
    elif cap is Capability.CONFIRMATION_RELIABILITY:
        answer = (f"{m['supplier_ref']}: confirms in {m['mean_confirm_latency_h']:g}h on average; "
                  f"promised date moved on {m['promise_change_rate']:.0%} of orders.")
    else:
        answer = ""
    gaps: list[str] = []
    if not need.meets_confidence(confidence):
        gaps.append("confidence below min_confidence")
    if m.get("n", 0) < 5:
        gaps.append("thin sample (<5 orders)")
    return answer, confidence, m, (), tuple(gaps)
