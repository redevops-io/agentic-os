"""Serve Asset Intelligence through the broker as an internal provider (Intelligence-APIs plan §10)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

from .asset import (
    asset_identity, failure_risk, maintenance_risk, parts_risk, replacement_compatibility, service_history,
)

_SERVES = (Capability.ASSET_IDENTITY, Capability.SERVICE_HISTORY, Capability.FAILURE_RISK,
           Capability.REPLACEMENT_COMPATIBILITY, Capability.MAINTENANCE_RISK, Capability.PARTS_RISK)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _params(subject_refs) -> dict[str, str]:
    d: dict[str, str] = {}
    pos: list[str] = []
    for s in subject_refs:
        if "=" in s:
            k, v = s.split("=", 1)
            d[k.strip()] = v.strip()
        else:
            pos.append(s)
    if pos:
        d.setdefault("asset", pos[0])
        d.setdefault("part", pos[0])
        d.setdefault("work", pos[0])
    return d


@dataclass
class AssetGraph:
    assets: list = field(default_factory=list)
    components: list = field(default_factory=list)
    work_orders: list = field(default_factory=list)
    service_events: list = field(default_factory=list)
    inventory: list = field(default_factory=list)
    as_of_ms: int = 0


@dataclass
class AssetIntelligenceProvider:
    graph: AssetGraph
    provider_id: str = "internal.asset_intelligence"
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
        cap = request.capability
        conf = 1.0
        if cap is Capability.ASSET_IDENTITY:
            r = asset_identity(p.get("asset", ""), g.assets, g.components, as_of_ms=g.as_of_ms)
            if r is None:
                return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "unknown asset")
            payload = asdict(r)
        elif cap is Capability.SERVICE_HISTORY:
            payload = asdict(service_history(p.get("asset", ""), g.service_events, as_of_ms=g.as_of_ms))
        elif cap is Capability.FAILURE_RISK:
            r = failure_risk(p.get("asset", ""), g.service_events,
                             horizon_days=float(p.get("horizon_days", "90")), as_of_ms=g.as_of_ms)
            payload, conf = asdict(r), r.confidence      # a forecast — confidence from sample size
        elif cap is Capability.MAINTENANCE_RISK:
            r = maintenance_risk(p.get("work", ""), g.work_orders, g.inventory, as_of_ms=g.as_of_ms)
            if r is None:
                return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "unknown work order")
            payload = asdict(r)
        elif cap is Capability.PARTS_RISK:
            payload = asdict(parts_risk(p.get("part", ""), g.components, g.service_events, g.inventory,
                                        as_of_ms=g.as_of_ms))
        elif cap is Capability.REPLACEMENT_COMPATIBILITY:
            opts = replacement_compatibility(p.get("part", ""), g.components, as_of_ms=g.as_of_ms)
            payload = {"part": p.get("part", ""), "count": len(opts), "options": [asdict(o) for o in opts]}
        else:
            return AcquisitionResult.failed(AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE, "not served")
        subject = p.get("asset") or p.get("part") or p.get("work") or ""
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=cap, subject=subject,
            observations=(payload,), retrieved_at=_now_iso(), freshness_s=0.0, confidence=conf, cost=0.0,
            license_scope="internal", raw_response_digest=content_hash(payload))
        return AcquisitionResult.found((art,), cost=0.0)


def asset_registry(graph: AssetGraph) -> IntelligenceRegistry:
    reg = IntelligenceRegistry()
    reg.register(AssetIntelligenceProvider(graph))
    return reg


def asset_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    cap, conf = need.capability, a.confidence
    gaps: tuple[str, ...] = ()
    if cap is Capability.ASSET_IDENTITY:
        answer = f"{m['asset_ref']}: {m['oem']} {m['model']} (s/n {m['serial']}), {len(m['components'])} component(s)."
    elif cap is Capability.SERVICE_HISTORY:
        answer = (f"{m['asset_ref']}: {len(m['events'])} service event(s), {m['failure_count']} failure(s), "
                  f"{m['total_downtime_hours']:g}h downtime.")
    elif cap is Capability.FAILURE_RISK:
        answer = (f"{m['asset_ref']}: P(failure in {m['horizon_days']:g}d) {m['p_failure_in_horizon']:.0%} "
                  f"({m['failure_rate_per_year']:g}/yr). Forecast.")
    elif cap is Capability.MAINTENANCE_RISK:
        answer = (f"{m['asset_ref']} work {m['work_ref']}: "
                  + ("on track (parts in stock)." if m["p_on_time"] == 1.0
                     else f"BLOCKED on parts {list(m['blocking_parts'])}."))
        gaps = () if m["p_on_time"] == 1.0 else ("maintenance blocked on parts",)
    elif cap is Capability.PARTS_RISK:
        answer = (f"{m['part']}: {m['installed_base']} installed, {m['annual_consumption']:g}/yr consumed, "
                  f"{m['on_hand_spares']:g} spares ({m['months_of_cover']:g} months cover).")
    elif cap is Capability.REPLACEMENT_COMPATIBILITY:
        answer = (f"{m['part']}: {m['count']} observed-compatible replacement(s)."
                  if m["count"] else f"{m['part']}: no observed compatible replacements.")
    else:
        answer = ""
    return answer, conf, m, (), gaps
