"""UK Companies House adapter — COMPANY_IDENTITY + BENEFICIAL_OWNERSHIP. BYO (free) key (§6 P0).

Companies House publishes the UK statutory register via a REST API that needs a (free) API key, sent as HTTP
basic auth with the key as the username and an empty password. Two capabilities off one register:
  * COMPANY_IDENTITY  — ``/search/companies`` (name → company number / status / incorporation).
  * BENEFICIAL_OWNERSHIP — ``/company/{number}/persons-with-significant-control`` (PSC = the UK's statutory
    beneficial-ownership record); the subject for this capability is the company number.
BYO like OpenCorporates (free to register, but keyed), so without a key it reports NOT entitled and the registry
falls through. Offline-testable via the injected GET ``fetch``.
"""
from __future__ import annotations

import base64
import time
from urllib.parse import quote

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, ProviderFamily, content_hash,
)

from ._http import Fetch, http_get_json


class CompaniesHouseProvider:
    provider_id = "companies_house"
    family = ProviderFamily.EXTERNAL_DATA
    _BASE = "https://api.company-information.service.gov.uk"

    def __init__(self, api_key: str = "", fetch: Fetch = http_get_json):
        self._key = api_key
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.COMPANY_IDENTITY, Capability.BENEFICIAL_OWNERSHIP)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=500)  # free key; cost is $0 but keyed entitlement

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in self.capabilities() and bool(self._key)  # BYO: no key ⇒ not entitled

    def _auth_header(self) -> dict:
        token = base64.b64encode(f"{self._key}:".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        if not self._key:
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, "no Companies House API key")
        subject = (request.subject_refs or ("",))[0]
        if not subject:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        bo = request.capability == Capability.BENEFICIAL_OWNERSHIP
        if bo:
            url = f"{self._BASE}/company/{quote(subject)}/persons-with-significant-control?items_per_page=10"
        else:
            url = f"{self._BASE}/search/companies?q={quote(subject)}&items_per_page=5"
        try:
            status, body = self._fetch(url, self._auth_header())
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        if status in (401, 403):
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, f"http {status}")
        if status >= 500 or status == 429:
            return AcquisitionResult.failed(
                AcquisitionFailure.RATE_LIMITED if status == 429 else AcquisitionFailure.UNAVAILABLE, f"http {status}")
        items = (body or {}).get("items") or []
        if not items:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, f"no result for {subject!r}")
        observations, refs = [], []
        if bo:
            for p in items[:10]:
                observations.append({
                    "name": p.get("name", ""), "kind": p.get("kind", ""),
                    "natures_of_control": p.get("natures_of_control", []),
                    "nationality": p.get("nationality", ""), "ceased": bool(p.get("ceased_on")),
                })
                refs.append(EvidenceRef(ref=f"companies_house:psc:{subject}:{p.get('name','')}",
                                        source="companies_house", content_hash=content_hash(p), ref_type="record"))
            cap = Capability.BENEFICIAL_OWNERSHIP
        else:
            for c in items[:5]:
                observations.append({
                    "title": c.get("title", ""), "company_number": c.get("company_number", ""),
                    "company_status": c.get("company_status", ""), "company_type": c.get("company_type", ""),
                    "date_of_creation": c.get("date_of_creation", ""),
                    "address_snippet": c.get("address_snippet", ""),
                })
                refs.append(EvidenceRef(ref=f"companies_house:{c.get('company_number','')}",
                                        source="companies_house", content_hash=content_hash(c), ref_type="record"))
            cap = Capability.COMPANY_IDENTITY
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=cap, subject=subject,
            observations=tuple(observations), source_refs=tuple(refs),
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), freshness_s=0.0,
            confidence=0.9 if len(observations) == 1 else 0.6, cost=0.0,
            license_scope="UK Companies House (Crown copyright / Open Government Licence)",
            raw_response_digest=content_hash(body), tenant=request.tenant)
        return AcquisitionResult.found((art,))
