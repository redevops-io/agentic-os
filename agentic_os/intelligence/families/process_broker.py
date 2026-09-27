"""Serve Process Intelligence through the broker as an internal provider (Intelligence-APIs plan §8).

Event-log analytics are computed from the tenant's OWN process events, so they resolve as a cost-0
internal provider (family INTERNAL_COMPUTED) through the same `resolve_decision_need` path as any external
provider. Capabilities that key on a case read it from the subject; NEXT_EVENT keys on an activity via a
"from=<activity>" (or bare) subject token; WHY_STUCK accepts an optional "now=<iso>".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

from .process import anomaly, bottlenecks, cycle_benchmark, next_event, why_stuck

_SERVES = (Capability.WHY_STUCK, Capability.BOTTLENECKS, Capability.CYCLE_BENCHMARK,
           Capability.PROCESS_ANOMALY, Capability.NEXT_EVENT)


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
        d.setdefault("case", pos[0])
        d.setdefault("from", pos[0])
    return d


@dataclass
class ProcessEventLog:
    """The tenant's process events, optionally scoped to what was knowable by `as_of_ms`."""
    events: list = field(default_factory=list)
    as_of_ms: int = 0


@dataclass
class ProcessIntelligenceProvider:
    log: ProcessEventLog
    provider_id: str = "internal.process_intelligence"
    family: ProviderFamily = ProviderFamily.INTERNAL_COMPUTED

    def capabilities(self) -> tuple[Capability, ...]:
        return _SERVES

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=1)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in _SERVES

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        ev, as_of = self.log.events, self.log.as_of_ms
        p = _params(request.subject_refs)
        cap = request.capability

        if cap is Capability.BOTTLENECKS:
            bl = bottlenecks(ev, as_of_ms=as_of)
            payload, conf = {"count": len(bl), "bottlenecks": [asdict(b) for b in bl]}, 1.0
        elif cap is Capability.CYCLE_BENCHMARK:
            payload, conf = asdict(cycle_benchmark(ev, p.get("case", ""), as_of_ms=as_of)), 1.0
        elif cap is Capability.NEXT_EVENT:
            r = next_event(ev, p.get("from", ""), as_of_ms=as_of)
            payload, conf = asdict(r), r.confidence
        elif cap is Capability.WHY_STUCK:
            payload, conf = asdict(why_stuck(ev, p.get("case", ""), now=p.get("now", ""), as_of_ms=as_of)), 1.0
        elif cap is Capability.PROCESS_ANOMALY:
            payload, conf = asdict(anomaly(ev, p.get("case", ""), as_of_ms=as_of)), 1.0
        else:
            return AcquisitionResult.failed(AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE,
                                            f"{cap.value} not served")

        subject = p.get("case") or p.get("from") or ""
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=cap, subject=subject,
            observations=(payload,), retrieved_at=_now_iso(), freshness_s=0.0, confidence=conf, cost=0.0,
            license_scope="internal", raw_response_digest=content_hash(payload))
        return AcquisitionResult.found((art,), cost=0.0)


def process_registry(log: ProcessEventLog) -> IntelligenceRegistry:
    reg = IntelligenceRegistry()
    reg.register(ProcessIntelligenceProvider(log))
    return reg


def process_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    cap, conf = need.capability, a.confidence
    gaps: tuple[str, ...] = ()
    if cap is Capability.BOTTLENECKS:
        if m["count"]:
            top = m["bottlenecks"][0]
            answer = (f"{m['count']} activit(y/ies) with waiting; worst: {top['activity']} "
                      f"({top['mean_wait_hours']:g}h avg over {top['n']}).")
        else:
            answer = "No waiting observed."
    elif cap is Capability.CYCLE_BENCHMARK:
        answer = (f"Case {m['case_ref']}: cycle {m['cycle_hours']:g}h, {m['percentile']:.0%} percentile "
                  f"vs {m['cohort_n']} peers (median {m['median_cohort_hours']:g}h).")
    elif cap is Capability.NEXT_EVENT:
        if m["expected_activity"]:
            answer = (f"After {m['from_activity']}: likely {m['expected_activity']} "
                      f"in ~{m['expected_hours']:g}h ({m['confidence']:.0%}).")
        else:
            answer = f"No observed transition after {m['from_activity']}."
    elif cap is Capability.WHY_STUCK:
        if m["stuck"]:
            answer = (f"Case {m['case_ref']} stuck at {m['current_activity']} for {m['elapsed_hours']:g}h "
                      f"(typical {m['typical_hours']:g}h); missing {m['missing_event']}; owner {m['owner']}.")
            gaps = ("stuck",)
        else:
            answer = f"Case {m['case_ref']} at {m['current_activity']} is progressing normally."
    elif cap is Capability.PROCESS_ANOMALY:
        if m["is_anomalous"]:
            answer = f"Case {m['case_ref']}: {len(m['findings'])} anomaly finding(s); {m['findings'][0][1]}."
            gaps = ("anomalous",)
        else:
            answer = f"Case {m['case_ref']}: no anomalies."
    else:
        answer = ""
    return answer, conf, m, (), gaps
