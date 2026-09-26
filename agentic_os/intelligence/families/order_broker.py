"""Serve Order Intelligence through the broker as an internal provider (Intelligence-APIs plan §7).

Order lineage, blockers and promise-feasibility are computed from the tenant's OWN canonical graph, so
they resolve as a cost-0 internal provider (family INTERNAL_COMPUTED) through the same
`resolve_decision_need` path as any external provider — receipt ledger, health, value accounting — and an
order synthesizer turns each into a business answer. `promise_feasibility` additionally reuses the
supplier `delivery_reliability` prior when a supply event source is supplied.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

from .order import InMemoryOrderGraph, order_blockers, order_lineage, promise_feasibility
from .supplier_broker import SupplyEventSource

_SERVES = (Capability.ORDER_LINEAGE, Capability.ORDER_BLOCKERS, Capability.PROMISE_FEASIBILITY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class OrderIntelligenceProvider:
    """Computes order lineage / blockers / promise-feasibility from the tenant's own graph — cost 0,
    always entitled. `supply_source` (optional) feeds the supplier on-time prior into promise-feasibility."""
    graph: InMemoryOrderGraph
    supply_source: SupplyEventSource | None = None
    provider_id: str = "internal.order_intelligence"
    family: ProviderFamily = ProviderFamily.INTERNAL_COMPUTED

    def capabilities(self) -> tuple[Capability, ...]:
        return _SERVES

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=1)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in _SERVES

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        order_ref = request.subject_refs[0] if request.subject_refs else ""
        cap = request.capability
        if self.graph.sales_order(order_ref) is None:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "unknown order")

        if cap is Capability.ORDER_LINEAGE:
            payload, confidence = asdict(order_lineage(order_ref, self.graph)), 1.0
        elif cap is Capability.ORDER_BLOCKERS:
            blocks = order_blockers(order_ref, self.graph)
            payload, confidence = {"order_ref": order_ref, "count": len(blocks),
                                   "blockers": [asdict(b) for b in blocks]}, 1.0
        elif cap is Capability.PROMISE_FEASIBILITY:
            pf = promise_feasibility(order_ref, self.graph, self.supply_source)
            payload, confidence = asdict(pf), pf.confidence
        else:
            return AcquisitionResult.failed(AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE,
                                            f"{cap.value} not served")
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=cap, subject=order_ref,
            observations=(payload,), retrieved_at=_now_iso(), freshness_s=0.0, confidence=confidence,
            cost=0.0, license_scope="internal", raw_response_digest=content_hash(payload))
        return AcquisitionResult.found((art,), cost=0.0)


def order_registry(graph: InMemoryOrderGraph, supply_source: SupplyEventSource | None = None) -> IntelligenceRegistry:
    reg = IntelligenceRegistry()
    reg.register(OrderIntelligenceProvider(graph, supply_source))
    return reg


def order_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    """The Order family's answer synthesis (plugged into resolve_decision_need)."""
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    cap, confidence = need.capability, a.confidence
    gaps: tuple[str, ...] = ()
    if cap is Capability.ORDER_LINEAGE:
        state = "complete" if m["complete"] else "in progress"
        answer = f"Order {m['order_ref']}: {state}; stages {', '.join(m['stages_present'])}."
        gaps = () if m["complete"] else ("order not yet complete",)
    elif cap is Capability.ORDER_BLOCKERS:
        n = m["count"]
        answer = ("No blockers — order is on track." if n == 0
                  else f"{n} blocker(s); most urgent: {m['blockers'][0]['message']}.")
    elif cap is Capability.PROMISE_FEASIBILITY:
        earliest = m["earliest_credible_date"] or "unbounded"
        answer = (f"Order {m['order_ref']}: P(on-time) {m['p_on_time']:.0%}, earliest credible {earliest}. "
                  f"{'; '.join(m['drivers'])}")
        gaps = () if need.meets_confidence(confidence) else ("confidence below min_confidence",)
    else:
        answer = ""
    return answer, confidence, m, (), gaps
