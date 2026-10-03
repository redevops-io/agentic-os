"""OpenFIGI adapter — ASSET_IDENTITY (financial-instrument identity via the FIGI graph). Open, key optional (§6 P0).

OpenFIGI (Bloomberg) maps a name/ticker to Financial Instrument Global Identifiers and the instrument's
exchange/security-type/market-sector. The mapping + search endpoints are callable keyless (low rate limit); a
free ``X-OPENFIGI-APIKEY`` raises the limit. So this is a base/open provider — always entitled — with the key
as an optional throughput boost, not an entitlement gate. POST transport (``/v3/search``), so it takes the full
``http_json`` seam rather than the GET-only one; fully offline-testable via the injected ``fetch``.
"""
from __future__ import annotations

import time

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, HealthStatus, ProviderFamily, ProviderHealth, content_hash,
)

from ._http import Fetch, http_json


class OpenFigiProvider:
    provider_id = "openfigi"
    family = ProviderFamily.EXTERNAL_DATA
    _BASE = "https://api.openfigi.com/v3"

    def __init__(self, api_key: str = "", fetch: Fetch = http_json):
        self._key = api_key
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.ASSET_IDENTITY,)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=400)  # open/free

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in self.capabilities()  # open provider — always entitled (key only raises rate limit)

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, HealthStatus.OK)

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        subject = (request.subject_refs or ("",))[0]
        if not subject:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["X-OPENFIGI-APIKEY"] = self._key
        try:
            status, body = self._fetch("POST", f"{self._BASE}/search", headers, {"query": subject})
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        if status in (401, 403):
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, f"http {status}")
        if status >= 500 or status == 429:
            return AcquisitionResult.failed(
                AcquisitionFailure.RATE_LIMITED if status == 429 else AcquisitionFailure.UNAVAILABLE, f"http {status}")
        rows = (body or {}).get("data") or []
        if not rows:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, f"no FIGI for {subject!r}")
        observations, refs = [], []
        for r in rows[:5]:
            figi = r.get("figi", "")
            observations.append({
                "figi": figi, "name": r.get("name", ""), "ticker": r.get("ticker", ""),
                "exch_code": r.get("exchCode", ""), "security_type": r.get("securityType", ""),
                "market_sector": r.get("marketSector", ""), "composite_figi": r.get("compositeFIGI", ""),
            })
            refs.append(EvidenceRef(ref=f"openfigi:{figi}", source="openfigi",
                                    content_hash=content_hash(r), ref_type="record"))
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=Capability.ASSET_IDENTITY,
            subject=subject, observations=tuple(observations), source_refs=tuple(refs),
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), freshness_s=0.0,
            confidence=0.9 if len(rows) == 1 else 0.6, cost=0.0, license_scope="OpenFIGI (open, attribution)",
            raw_response_digest=content_hash(body), tenant=request.tenant)
        return AcquisitionResult.found((art,))
