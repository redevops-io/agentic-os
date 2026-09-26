"""Supplier resolution — resolve_supplier (Intelligence-APIs plan §5, Table 1; Partnership Strategy §7/§11).

Resolve a name / domain / tax id / LEI to a canonical supplier, under the strategy's routing policy:
**internal ERP records first, GLEIF/open reference data next, paid firmographic only when it materially
exceeds the free primitives.** That policy is realised through the ordinary broker ladder — the internal
resolver is cost-0 and, when it cannot match confidently, returns LOW_CONFIDENCE (a retryable failure) so
`resolve_decision_need` falls through to the GLEIF (open) resolver. Paid providers register on top later.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceProvider, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

from ...integrations.business.supply import Supplier

_EXACT_ID_KEYS = ("lei", "tax_id", "duns")


@dataclass(frozen=True)
class SupplierResolution:
    query: str
    supplier_ref: str          # canonical (ERP) ref; "" when resolved only against an external source
    name: str
    lei: str
    matched_on: str            # lei | tax_id | duns | domain | name_exact | name_fuzzy | external
    confidence: float
    tier: str                  # internal | open


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _tokens(s: str) -> set[str]:
    return set(_norm(s).split())


# ── internal ERP entity resolution (Tier 0 · internal, cost 0) ────────────────────────────────────────────
def resolve_supplier(query: str, suppliers) -> SupplierResolution | None:
    """Best-match a single identifier (LEI / tax id / DUNS / domain / name) against the tenant's own
    Supplier records. Exact identifier > exact name > fuzzy name (token Jaccard). None if nothing matches."""
    q = query.strip()
    if not q:
        return None
    qn, qt = _norm(q), _tokens(q)
    best: SupplierResolution | None = None
    for s in suppliers:
        ext = {k.lower(): str(v) for k, v in (s.external_ids or {}).items()}
        matched, conf = "", 0.0
        for key in _EXACT_ID_KEYS:
            if ext.get(key, "").lower() == q.lower() and ext.get(key):
                matched, conf = key, 0.99
                break
        if not matched and ext.get("domain", "").lower() == q.lower() and ext.get("domain"):
            matched, conf = "domain", 0.9
        if not matched and qn and _norm(s.name) == qn:
            matched, conf = "name_exact", 0.85
        if not matched and qt and _tokens(s.name):
            j = len(qt & _tokens(s.name)) / len(qt | _tokens(s.name))
            if j > 0:
                matched, conf = "name_fuzzy", round(0.5 + 0.35 * j, 4)
        if matched and (best is None or conf > best.confidence):
            best = SupplierResolution(query=q, supplier_ref=s.prov.provider_ref, name=s.name,
                                      lei=ext.get("lei", ""), matched_on=matched, confidence=conf,
                                      tier="internal")
    return best


def _artifact(provider: str, family: ProviderFamily, subject: str, res: SupplierResolution, *,
              cost: float = 0.0, retrieved_at: str = "", license_scope: str = "internal",
              source_refs=()) -> EvidenceArtifact:
    payload = asdict(res)
    return EvidenceArtifact(
        provider=provider, family=family, capability=Capability.SUPPLIER_RESOLUTION, subject=subject,
        observations=(payload,), source_refs=tuple(source_refs), retrieved_at=retrieved_at or _now_iso(),
        freshness_s=0.0, confidence=res.confidence, cost=cost, license_scope=license_scope,
        raw_response_digest=content_hash(payload))


@dataclass
class InternalSupplierResolver:
    """ERP entity resolution — cost 0, always entitled. Returns LOW_CONFIDENCE (retryable) when it cannot
    match confidently, so the broker ladder falls through to the open resolver."""
    suppliers: list
    min_confidence: float = 0.75
    provider_id: str = "internal.supplier_resolution"
    family: ProviderFamily = ProviderFamily.INTERNAL_COMPUTED

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.SUPPLIER_RESOLUTION,)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=1)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability is Capability.SUPPLIER_RESOLUTION

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        q = request.subject_refs[0] if request.subject_refs else ""
        res = resolve_supplier(q, self.suppliers)
        if res is None or res.confidence < self.min_confidence:
            return AcquisitionResult.failed(AcquisitionFailure.LOW_CONFIDENCE,
                                            "no confident internal match", cost=0.0)
        return AcquisitionResult.found((_artifact(self.provider_id, self.family, q, res),), cost=0.0)


@dataclass
class GleifSupplierResolver:
    """Open resolver — wraps a COMPANY_IDENTITY provider (GLEIF) and maps its legal-entity record to a
    supplier resolution. GLEIF is CC0/open, so this is a free primitive, not a paid dependency."""
    identity: IntelligenceProvider
    provider_id: str = "open.gleif_supplier_resolution"
    family: ProviderFamily = ProviderFamily.EXTERNAL_DATA

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.SUPPLIER_RESOLUTION,)

    def _as_identity(self, request: EvidenceRequest) -> EvidenceRequest:
        return EvidenceRequest(
            decision_case_id=request.decision_case_id, capability=Capability.COMPANY_IDENTITY,
            subject_refs=request.subject_refs, fields=request.fields, purpose=request.purpose,
            tenant=request.tenant, max_cost=request.max_cost, max_age_s=request.max_age_s,
            jurisdiction=request.jurisdiction, sensitivity=request.sensitivity)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return self.identity.estimate_cost(self._as_identity(request))

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return (capability is Capability.SUPPLIER_RESOLUTION
                and self.identity.check_entitlement(tenant, Capability.COMPANY_IDENTITY))

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        q = request.subject_refs[0] if request.subject_refs else ""
        res = self.identity.acquire(self._as_identity(request))
        if not res.ok or not res.artifacts:
            return AcquisitionResult.failed(res.failure or AcquisitionFailure.NO_MATCH, res.detail,
                                            cost=res.cost)
        a = res.artifacts[0]
        obs = dict(a.observations[0]) if a.observations else {}
        resr = SupplierResolution(query=q, supplier_ref="", name=obs.get("legal_name") or a.subject,
                                  lei=obs.get("lei", ""), matched_on="external", confidence=a.confidence,
                                  tier="open")
        art = _artifact(self.provider_id, self.family, q, resr, cost=res.cost,
                        retrieved_at=a.retrieved_at, license_scope=a.license_scope, source_refs=a.source_refs)
        return AcquisitionResult.found((art,), cost=res.cost)


def supplier_resolution_registry(suppliers, identity: IntelligenceProvider | None = None) -> IntelligenceRegistry:
    """The routing ladder: internal ERP resolution first, then GLEIF/open when supplied. Both cost 0, so
    the registry keeps registration order — internal, then open."""
    reg = IntelligenceRegistry()
    reg.register(InternalSupplierResolver(list(suppliers)))
    if identity is not None:
        reg.register(GleifSupplierResolver(identity))
    return reg


def supplier_resolution_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    """State the resolved entity, how it was matched and at what tier/confidence."""
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    conf = a.confidence
    lei = f" (LEI {m['lei']})" if m.get("lei") else ""
    answer = (f"'{m['query']}' → {m['name']}{lei} "
              f"[{m['tier']}/{m['matched_on']}, confidence {conf:.0%}]")
    gaps = () if need.meets_confidence(conf) else ("confidence below min_confidence",)
    return answer, conf, m, (), gaps
