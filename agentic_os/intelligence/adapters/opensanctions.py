"""OpenSanctions adapter — SANCTIONS_RISK (sanctions / PEP / risk screening). §6 P0 base provider.

Two deployment modes: the hosted api.opensanctions.org (requires an API key) or a self-hosted yente instance
(open, no key). Entitlement is true when a key is configured OR a self-hosted base URL is set — so the open
stack can run screening against yente with no paid key.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, ProviderFamily, content_hash,
)

from ._http import Fetch, http_get_json

_HOSTED = "https://api.opensanctions.org"


class OpenSanctionsProvider:
    provider_id = "opensanctions"
    family = ProviderFamily.EXTERNAL_DATA

    def __init__(self, api_key: str = "", base_url: str = _HOSTED, dataset: str = "default",
                 fetch: Fetch = http_get_json):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._dataset = dataset
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.SANCTIONS_RISK,)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        # hosted API is credit-metered; self-hosted yente is free.
        return CostEstimate(money=0.0 if self._self_hosted else 0.01, latency_ms=400)

    @property
    def _self_hosted(self) -> bool:
        return self._base != _HOSTED

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        if capability not in self.capabilities():
            return False
        return bool(self._key) or self._self_hosted   # need a key for hosted; yente is open

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        name = (request.subject_refs or ("",))[0]
        if not name:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        url = f"{self._base}/search/{self._dataset}?q={quote(name)}&limit=5"
        headers = {"Accept": "application/json"}
        if self._key:
            headers["Authorization"] = f"ApiKey {self._key}"
        try:
            status, body = self._fetch(url, headers)
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        if status in (401, 403):
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, f"http {status}")
        if status >= 500 or status == 429:
            return AcquisitionResult.failed(
                AcquisitionFailure.RATE_LIMITED if status == 429 else AcquisitionFailure.UNAVAILABLE, f"http {status}")
        results = (body or {}).get("results") or []
        cost = self.estimate_cost(request).money
        if not results:
            # a clean "no hit" is decision-relevant evidence (screened, nothing found) — return it, not a failure.
            art = EvidenceArtifact(
                provider=self.provider_id, family=self.family, capability=Capability.SANCTIONS_RISK,
                subject=name, observations=({"match": False, "hits": 0},), source_refs=(),
                retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), freshness_s=0.0,
                confidence=0.9, cost=cost, license_scope="OpenSanctions (CC-BY-NC)",
                raw_response_digest=content_hash(body), tenant=request.tenant)
            return AcquisitionResult.found((art,), cost=cost)
        observations, refs = [], []
        top = 0.0
        for r in results[:5]:
            score = float(r.get("score", 0.0) or 0.0)
            top = max(top, score)
            observations.append({
                "id": r.get("id", ""), "caption": r.get("caption", ""), "schema": r.get("schema", ""),
                "score": score, "datasets": r.get("datasets", []),
                "topics": (r.get("properties", {}) or {}).get("topics", []),
            })
            refs.append(EvidenceRef(ref=f"opensanctions:{r.get('id','')}", source="opensanctions",
                                    content_hash=content_hash(r), ref_type="record"))
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=Capability.SANCTIONS_RISK,
            subject=name, observations=({"match": True, "hits": len(results)},) + tuple(observations),
            source_refs=tuple(refs), retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            freshness_s=0.0, confidence=min(0.99, top), cost=cost,
            license_scope="OpenSanctions (CC-BY-NC)", raw_response_digest=content_hash(body),
            tenant=request.tenant)
        return AcquisitionResult.found((art,), cost=cost)
