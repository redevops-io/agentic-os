"""Open Ownership adapter — BENEFICIAL_OWNERSHIP (global, BODS). Open (§6 P0).

The Open Ownership Register reconciles national beneficial-ownership datasets (DK/SK/UA/UK + OpenCorporates) into
the Beneficial Ownership Data Standard (BODS) and is searchable by person, company name, or company id. It is
open data (no entitlement gate); an optional token/base-url lets a deployment point at the hosted register, a
self-hosted mirror, or the BODS/Datasette analysis endpoint.

NOTE: Open Ownership's primary machine path is bulk BODS + a Datasette/BigQuery analysis layer
(``bods-data.openownership.org``); a live per-query JSON endpoint should be confirmed for the target deployment.
This adapter normalizes a BODS-shaped search response and is fully offline-testable via the injected GET
``fetch`` — the only live-dependent part is the base URL constant.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, HealthStatus, ProviderFamily, ProviderHealth, content_hash,
)

from ._http import Fetch, http_get_json

_HOSTED = "https://register.openownership.org"


class OpenOwnershipProvider:
    provider_id = "open_ownership"
    family = ProviderFamily.EXTERNAL_DATA

    def __init__(self, api_token: str = "", base_url: str = _HOSTED, fetch: Fetch = http_get_json):
        self._token = api_token
        self._base = base_url.rstrip("/")
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.BENEFICIAL_OWNERSHIP,)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=600)  # open data

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in self.capabilities()  # open provider — always entitled

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, HealthStatus.OK)

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        name = (request.subject_refs or ("",))[0]
        if not name:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        url = f"{self._base}/search.json?q={quote(name)}"
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            status, body = self._fetch(url, headers)
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        if status in (401, 403):
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, f"http {status}")
        if status >= 500 or status == 429:
            return AcquisitionResult.failed(
                AcquisitionFailure.RATE_LIMITED if status == 429 else AcquisitionFailure.UNAVAILABLE, f"http {status}")
        # BODS-shaped search: a list of statements/records (hosted register returns {"results":[...]}).
        results = (body or {}).get("results") or (body or {}).get("statements") or []
        if not results:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, f"no ownership record for {name!r}")
        observations, refs = [], []
        for r in results[:10]:
            observations.append({
                "statement_id": r.get("statementID", r.get("id", "")),
                "entity": r.get("name", (r.get("entity", {}) or {}).get("name", "")),
                "statement_type": r.get("statementType", r.get("type", "")),
                "interests": r.get("interests", []),
                "jurisdiction": r.get("jurisdiction", (r.get("entity", {}) or {}).get("jurisdiction", "")),
            })
            refs.append(EvidenceRef(ref=f"open_ownership:{r.get('statementID', r.get('id',''))}",
                                    source="open_ownership", content_hash=content_hash(r), ref_type="record"))
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=Capability.BENEFICIAL_OWNERSHIP,
            subject=name, observations=tuple(observations), source_refs=tuple(refs),
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), freshness_s=0.0,
            confidence=0.85 if len(results) == 1 else 0.6, cost=0.0,
            license_scope="Open Ownership Register (open data)", raw_response_digest=content_hash(body),
            tenant=request.tenant)
        return AcquisitionResult.found((art,))
