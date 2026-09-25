"""OpenCorporates adapter — COMPANY_IDENTITY (public registry/company/officer graph). §6 P0, BYO token.

Sits between open GLEIF and premium D&B. Commercial use requires an API token, so this is a Bring-Your-Own
provider: without a token it reports NOT entitled and the registry falls through to open providers.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, ProviderFamily, content_hash,
)

from ._http import Fetch, http_get_json


class OpenCorporatesProvider:
    provider_id = "opencorporates"
    family = ProviderFamily.EXTERNAL_DATA
    _BASE = "https://api.opencorporates.com/v0.4"

    def __init__(self, api_token: str = "", fetch: Fetch = http_get_json):
        self._token = api_token
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.COMPANY_IDENTITY, Capability.CORPORATE_HIERARCHY)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.02, latency_ms=500)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in self.capabilities() and bool(self._token)  # BYO: no token ⇒ not entitled

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        if not self._token:
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, "no OpenCorporates API token")
        name = (request.subject_refs or ("",))[0]
        if not name:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        juris = f"&jurisdiction_code={quote(request.jurisdiction)}" if request.jurisdiction else ""
        url = f"{self._BASE}/companies/search?q={quote(name)}{juris}&api_token={quote(self._token)}&per_page=5"
        try:
            status, body = self._fetch(url, {"Accept": "application/json"})
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        if status in (401, 403):
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, f"http {status}")
        if status >= 500 or status == 429:
            return AcquisitionResult.failed(
                AcquisitionFailure.RATE_LIMITED if status == 429 else AcquisitionFailure.UNAVAILABLE, f"http {status}")
        companies = ((body or {}).get("results") or {}).get("companies") or []
        if not companies:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, f"no company for {name!r}")
        observations, refs = [], []
        for item in companies[:5]:
            c = item.get("company", {}) or {}
            observations.append({
                "name": c.get("name", ""), "company_number": c.get("company_number", ""),
                "jurisdiction_code": c.get("jurisdiction_code", ""),
                "incorporation_date": c.get("incorporation_date", ""),
                "current_status": c.get("current_status", ""), "opencorporates_url": c.get("opencorporates_url", ""),
            })
            refs.append(EvidenceRef(
                ref=f"opencorporates:{c.get('jurisdiction_code','')}:{c.get('company_number','')}",
                source="opencorporates", content_hash=content_hash(c), ref_type="record"))
        cost = self.estimate_cost(request).money
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=Capability.COMPANY_IDENTITY,
            subject=name, observations=tuple(observations), source_refs=tuple(refs),
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), freshness_s=0.0,
            confidence=0.9 if len(companies) == 1 else 0.6, cost=cost,
            license_scope="OpenCorporates (licensed)", raw_response_digest=content_hash(body),
            tenant=request.tenant)
        return AcquisitionResult.found((art,), cost=cost)
