"""DecisionNeed → IntelligenceResult resolution (Intelligence-APIs plan §2, §3).

`acquire_for_decision` (in discovery_bridge) resolves a single EvidenceRequest. This resolves the layer
above it: a business `DecisionNeed`, walking the fallback ladder across entitled providers and aggregating
what it acquires into one governed `IntelligenceResult` — with a per-call `ProviderReceipt` ledger (so
provider cost is never hidden, §4.6), a total spend derived from those receipts, and the bi-temporal
decision anchor carried through so the answer is replayable.

It reuses the contract-level gate (`decide_acquire`) and honours the need's policy envelope: providers the
need does not permit, or whose `health()` reports UNAVAILABLE, are routed around before any spend.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from runtime_contracts.protocol import (
    AcquisitionFailure, Capability, DecisionNeed, EvidenceArtifact, EvidenceValueRecord, HealthStatus,
    IntelligenceRegistry, IntelligenceResult, PlannerFallback, ProviderReceipt, decide_acquire,
    provider_health,
)
from .value_store import EvidenceValueStore

# Failures worth trying the next provider for (mirrors the registry's ladder policy); everything else stops it.
_RETRYABLE = frozenset({
    AcquisitionFailure.UNAVAILABLE, AcquisitionFailure.RATE_LIMITED, AcquisitionFailure.NO_MATCH,
    AcquisitionFailure.LOW_CONFIDENCE, AcquisitionFailure.STALE, AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE,
})

# A synthesizer turns the acquired evidence into (answer, confidence, metrics, assumptions, gaps). The default
# is domain-neutral; each intelligence family supplies its own so the answer reflects the business question.
Synthesis = tuple[str, float, dict[str, Any], tuple[str, ...], tuple[str, ...]]
Synthesizer = Callable[[DecisionNeed, tuple[EvidenceArtifact, ...]], Synthesis]


def default_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]) -> Synthesis:
    """Neutral aggregation: best-evidence confidence, a lineage summary, and the gaps the envelope implies.
    Families override this to produce a real business answer; the broker never invents prose."""
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    confidence = max(a.confidence for a in artifacts)
    providers = tuple(sorted({a.provider for a in artifacts}))
    metrics: dict[str, Any] = {"artifact_count": len(artifacts), "providers": list(providers)}
    gaps = () if need.meets_confidence(confidence) else ("confidence below min_confidence",)
    return "", confidence, metrics, (), gaps


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expiry(produced_at: str, ttl_s: float) -> str:
    if not ttl_s or not produced_at:
        return ""
    try:
        base = datetime.fromisoformat(produced_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (base + timedelta(seconds=ttl_s)).astimezone(timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_decision_need(
    registry: IntelligenceRegistry,
    need: DecisionNeed,
    *,
    synthesize: Synthesizer = default_synthesize,
    value_fn: Callable[[Any], float] = lambda _r: 1.0,
    value_threshold: float = 0.1,
    store: Optional[EvidenceValueStore] = None,
    produced_at: str = "",
    ttl_s: float = 0.0,
) -> tuple[IntelligenceResult, list[str]]:
    """Resolve a DecisionNeed into a governed IntelligenceResult (+ a human-readable trace).

    Ladder: match entitled providers cheapest-first → drop those the need forbids or whose health is
    UNAVAILABLE → gate each (entitlement/PII/budget/value) → acquire → stop on the first success or a
    non-retryable failure. Every provider call yields a ProviderReceipt; total spend is derived from them.
    """
    request = need.to_evidence_request()
    produced_at = produced_at or _now_iso()
    trace: list[str] = []

    # candidate selection: capability + entitlement + cheapest-first (registry.match), then the need's
    # policy envelope and provider health.
    candidates = []
    for p in registry.match(request):
        if not need.permits_provider(p.provider_id):
            trace.append(f"{p.provider_id}: excluded (not a permitted provider)")
            continue
        h = provider_health(p)
        if h.status is HealthStatus.UNAVAILABLE:
            trace.append(f"{p.provider_id}: excluded (health UNAVAILABLE — {h.detail})")
            continue
        candidates.append(p)

    value = value_fn(request)
    receipts: list[ProviderReceipt] = []
    artifacts: tuple[EvidenceArtifact, ...] = ()
    spent = 0.0
    success_provider = ""
    last_failure = AcquisitionFailure.NO_MATCH

    for p in candidates:
        cost = p.estimate_cost(request)
        gate = decide_acquire(request, entitled=True, cost=cost, value_estimate=value,
                              value_threshold=value_threshold)
        if not gate.acquire:
            trace.append(f"{p.provider_id}: skip ({gate.reason})")
            if gate.fallback == PlannerFallback.TRY_ALTERNATE_PROVIDER or gate.failure == AcquisitionFailure.NOT_ENTITLED:
                continue
            last_failure = gate.failure or AcquisitionFailure.BUDGET_EXCEEDED
            break  # budget/PII/value gates stop the ladder — never silently substitute a weaker provider
        res = p.acquire(request)
        spent += res.cost
        if res.ok and res.artifacts:
            receipts.append(ProviderReceipt(
                provider=p.provider_id, capability=request.capability, cost=res.cost, ok=True,
                artifact_id=res.artifacts[0].identity() if len(res.artifacts) == 1 else "",
                retrieved_at=res.artifacts[0].retrieved_at, license_scope=res.artifacts[0].license_scope))
            artifacts = res.artifacts
            success_provider = p.provider_id
            trace.append(f"{p.provider_id}: acquired {len(res.artifacts)} artifact(s), cost {res.cost}")
            break
        last_failure = res.failure or AcquisitionFailure.NO_MATCH
        receipts.append(ProviderReceipt.for_failure(p.provider_id, request.capability, last_failure,
                                                    cost=res.cost, detail=res.detail))
        trace.append(f"{p.provider_id}: {last_failure.value} ({res.detail})")
        if last_failure not in _RETRYABLE:
            break

    if not candidates:
        trace.append("no permitted, healthy, entitled provider for capability")

    answer, confidence, metrics, assumptions, gaps = synthesize(need, artifacts)
    result = IntelligenceResult.from_artifacts(
        need, artifacts, tuple(receipts), answer=answer, confidence=confidence, metrics=metrics,
        assumptions=assumptions, unresolved_gaps=gaps, produced_at=produced_at,
        expires_at=_expiry(produced_at, ttl_s))

    if store is not None:
        store.append(EvidenceValueRecord(
            decision_case_id=need.decision_case_id, capability=request.capability, provider=success_provider,
            evidence_requested=True, evidence_received=bool(artifacts), cost=round(spent, 6)))

    return result, trace
