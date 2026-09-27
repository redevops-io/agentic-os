"""Serve Supply Intelligence through the broker as an internal provider (Intelligence-APIs plan §6).

Shortage, feasibility, BOM impact, substitutes, stockout consequence and safety stock are computed from
the tenant's OWN canonical graph, so they resolve as a cost-0 internal provider (family INTERNAL_COMPUTED)
through the same `resolve_decision_need` path as any external provider. A supply synthesizer states each
answer, and — honouring §6 — a forecast (safety stock) is reported as a recommendation, distinct from the
hard feasibility facts.

Capabilities that need parameters beyond the subject (qty / need_by / lead_time_days / service_level) read
them from `subject_refs` "key=value" tokens, e.g. ("part=Rim", "site=A", "qty=100", "need_by=2026-03-01").
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

from .supplier_broker import SupplyEventSource
from .supply import (
    bom_impact, required_by_feasibility, safety_stock, shortage_risk, stockout_consequence,
    substitute_availability,
)

_SERVES = (Capability.SHORTAGE_RISK, Capability.REQUIRED_BY_FEASIBILITY, Capability.BOM_IMPACT,
           Capability.SUBSTITUTE_AVAILABILITY, Capability.STOCKOUT_CONSEQUENCE, Capability.SAFETY_STOCK)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _params(subject_refs) -> dict[str, str]:
    """Parse ("key=value", …) tokens; a bare first token is the part, a bare second is the site."""
    d: dict[str, str] = {}
    pos: list[str] = []
    for s in subject_refs:
        if "=" in s:
            k, v = s.split("=", 1)
            d[k.strip()] = v.strip()
        else:
            pos.append(s)
    if pos and "part" not in d:
        d["part"] = pos[0]
    if len(pos) > 1 and "site" not in d:
        d["site"] = pos[1]
    return d


@dataclass
class SupplyGraph:
    """The tenant's supply/inventory/BOM/demand objects. `supply_source` feeds the supplier on-time prior
    into required_by_feasibility; `as_of_ms` scopes to what was knowable at a decision time."""
    inventory: list = field(default_factory=list)
    open_supply: list = field(default_factory=list)     # PurchaseOrders still open
    demand: list = field(default_factory=list)          # DemandRequirements
    bom_lines: list = field(default_factory=list)
    order_lines: list = field(default_factory=list)     # SalesOrderLines
    sales_orders: list = field(default_factory=list)
    supply_source: SupplyEventSource | None = None
    as_of_ms: int = 0


@dataclass
class SupplyIntelligenceProvider:
    """Cost-0, always-entitled internal provider serving the Supply family from the tenant's own graph."""
    graph: SupplyGraph
    provider_id: str = "internal.supply_intelligence"
    family: ProviderFamily = ProviderFamily.INTERNAL_COMPUTED

    def capabilities(self) -> tuple[Capability, ...]:
        return _SERVES

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=1)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in _SERVES

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        g = self.graph
        p = _params(request.subject_refs)
        part, site = p.get("part", ""), p.get("site", "")
        cap = request.capability
        if not part:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no part in subject")

        if cap is Capability.SHORTAGE_RISK:
            payload, conf = asdict(shortage_risk(part, site, g.inventory, g.open_supply, g.demand,
                                                 as_of_ms=g.as_of_ms)), 1.0
        elif cap is Capability.REQUIRED_BY_FEASIBILITY:
            r = required_by_feasibility(part, float(p.get("qty", "0")), p.get("need_by", ""), site,
                                        g.inventory, g.open_supply, as_of_ms=g.as_of_ms,
                                        supply_source=g.supply_source)
            payload, conf = asdict(r), r.confidence
        elif cap is Capability.BOM_IMPACT:
            payload, conf = asdict(bom_impact(part, g.bom_lines, g.order_lines, as_of_ms=g.as_of_ms)), 1.0
        elif cap is Capability.SUBSTITUTE_AVAILABILITY:
            opts = substitute_availability(part, g.bom_lines, g.inventory, site=site, as_of_ms=g.as_of_ms)
            payload, conf = {"part": part, "count": len(opts), "substitutes": [asdict(o) for o in opts]}, 1.0
        elif cap is Capability.STOCKOUT_CONSEQUENCE:
            payload, conf = asdict(stockout_consequence(part, g.bom_lines, g.order_lines, g.sales_orders,
                                                        as_of_ms=g.as_of_ms)), 1.0
        elif cap is Capability.SAFETY_STOCK:
            r = safety_stock(part, site, g.demand, lead_time_days=float(p.get("lead_time_days", "0")),
                             service_level=float(p.get("service_level", "0.95")))
            payload, conf = asdict(r), (0.5 if r.n >= 2 else 0.0)     # a forecast — modest confidence
        else:
            return AcquisitionResult.failed(AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE,
                                            f"{cap.value} not served")

        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=cap, subject=part,
            observations=(payload,), retrieved_at=_now_iso(), freshness_s=0.0, confidence=conf, cost=0.0,
            license_scope="internal", raw_response_digest=content_hash(payload))
        return AcquisitionResult.found((art,), cost=0.0)


def supply_registry(graph: SupplyGraph) -> IntelligenceRegistry:
    reg = IntelligenceRegistry()
    reg.register(SupplyIntelligenceProvider(graph))
    return reg


def supply_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    """The Supply family's answer synthesis (plugged into resolve_decision_need)."""
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    cap, conf = need.capability, a.confidence
    gaps: tuple[str, ...] = ()
    if cap is Capability.SHORTAGE_RISK:
        if m["first_shortage_date"]:
            answer = (f"{m['part']}: first shortage {m['first_shortage_date']} "
                      f"(min projected balance {m['min_projected_balance']:g}, "
                      f"{len(m['shortages'])} short date(s)).")
            gaps = ("projected shortage",)
        else:
            answer = f"{m['part']}: no projected shortage (min balance {m['min_projected_balance']:g})."
    elif cap is Capability.REQUIRED_BY_FEASIBILITY:
        verdict = "feasible" if m["feasible"] else "NOT feasible"
        earliest = m["earliest_credible_date"] if "earliest_credible_date" in m else m["earliest_reliable_date"]
        answer = (f"{m['part']} ×{m['required_qty']:g} by {m['need_by']}: {verdict} "
                  f"(available {m['available_by_need']:g}, earliest {earliest or 'unbounded'}, "
                  f"P(on-time) {m['p_on_time']:.0%}).")
        gaps = () if m["feasible"] else ("required-by date not feasible from committed supply",)
    elif cap is Capability.BOM_IMPACT:
        answer = (f"{m['changed_part']}: affects {len(m['affected_assemblies'])} assembl(y/ies) and "
                  f"{len(m['affected_orders'])} open order(s).")
    elif cap is Capability.SUBSTITUTE_AVAILABILITY:
        if m["count"]:
            top = m["substitutes"][0]
            answer = f"{m['part']}: {m['count']} approved substitute(s); best {top['part']} ({top['on_hand']:g} on hand)."
        else:
            answer = f"{m['part']}: no approved substitutes."
            gaps = ("no qualified substitute",)
    elif cap is Capability.STOCKOUT_CONSEQUENCE:
        answer = (f"{m['part']} stockout delays {m['orders_delayed']} order(s)"
                  + (f", soonest promise {m['earliest_impact_date']}." if m["earliest_impact_date"] else "."))
    elif cap is Capability.SAFETY_STOCK:
        answer = (f"{m['part']}: recommend safety stock {m['recommended_safety_stock']:g} "
                  f"(service {m['service_level']:.0%}, lead {m['lead_time_days']:g}d, n={m['n']}). "
                  "Forecast recommendation.")
        if m["n"] < 2:
            gaps = ("insufficient demand history",)
    else:
        answer = ""
    return answer, conf, m, (), gaps
