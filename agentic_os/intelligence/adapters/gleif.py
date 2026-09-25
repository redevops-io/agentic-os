"""GLEIF adapter — COMPANY_IDENTITY (legal-entity identity via the LEI record graph). Open, no key (§6 P0).

GLEIF publishes the global Legal Entity Identifier register under CC0. This is a base/open provider: the open
Agentic Apps stack ships it with no credentials.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, ProviderFamily, content_hash,
)

from ._http import Fetch, http_get_json


class GleifProvider:
    provider_id = "gleif"
    family = ProviderFamily.EXTERNAL_DATA
    _BASE = "https://api.gleif.org/api/v1"

    def __init__(self, fetch: Fetch = http_get_json):
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.COMPANY_IDENTITY,)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=300)  # open/free

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in self.capabilities()  # open provider — always entitled

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        name = (request.subject_refs or ("",))[0]
        if not name:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        url = f"{self._BASE}/lei-records?filter[entity.legalName]={quote(name)}&page[size]=5"
        try:
            status, body = self._fetch(url, {"Accept": "application/vnd.api+json"})
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        if status >= 500 or status == 429:
            return AcquisitionResult.failed(
                AcquisitionFailure.RATE_LIMITED if status == 429 else AcquisitionFailure.UNAVAILABLE, f"http {status}")
        recs = (body or {}).get("data") or []
        if not recs:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, f"no LEI for {name!r}")
        observations, refs = [], []
        for rec in recs[:5]:
            attr = rec.get("attributes", {}) or {}
            ent = attr.get("entity", {}) or {}
            lei = attr.get("lei", "")
            observations.append({
                "lei": lei,
                "legal_name": (ent.get("legalName") or {}).get("name", ""),
                "status": ent.get("status", ""),
                "jurisdiction": ent.get("jurisdiction", ""),
                "registration_status": (attr.get("registration") or {}).get("status", ""),
            })
            refs.append(EvidenceRef(ref=f"gleif:lei:{lei}", source="gleif", content_hash=content_hash(rec),
                                    ref_type="record"))
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=Capability.COMPANY_IDENTITY,
            subject=name, observations=tuple(observations), source_refs=tuple(refs),
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), freshness_s=0.0,
            confidence=0.95 if len(recs) == 1 else 0.6, cost=0.0, license_scope="GLEIF (CC0)",
            raw_response_digest=content_hash(body), tenant=request.tenant)
        return AcquisitionResult.found((art,))
