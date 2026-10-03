"""SEC EDGAR adapter — COMPANY_IDENTITY for US public companies (CIK / ticker / filings presence). Open (§6 P0).

The SEC's EDGAR full-text search API (``efts.sec.gov``) is public-domain and keyless; the SEC only requires a
descriptive ``User-Agent`` naming the caller + a contact. This resolves a company name to its CIK, tickers, and
recent-filing presence — the US-registry counterpart to GLEIF/OpenCorporates for the Counterparty family. Open,
always entitled; the one deployment knob is the User-Agent (defaulted, overridable). Offline-testable via the
injected GET ``fetch``.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, HealthStatus, ProviderFamily, ProviderHealth, content_hash,
)

from ._http import Fetch, http_get_json

# The SEC mandates a UA identifying the requester with a contact; a deployment should set its own.
_DEFAULT_UA = "ReDevOps Agentic Apps (intelligence@redevops.io)"


class SecEdgarProvider:
    provider_id = "sec_edgar"
    family = ProviderFamily.EXTERNAL_DATA
    _BASE = "https://efts.sec.gov/LATEST/search-index"

    def __init__(self, user_agent: str = _DEFAULT_UA, fetch: Fetch = http_get_json):
        self._ua = user_agent or _DEFAULT_UA
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return (Capability.COMPANY_IDENTITY,)

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=500)  # open/free (US public domain)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in self.capabilities()  # open provider — always entitled

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, HealthStatus.OK)

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        name = (request.subject_refs or ("",))[0]
        if not name:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        phrase = '"' + name + '"'              # exact-phrase match on the legal name
        url = f"{self._BASE}?q={quote(phrase)}"
        try:
            status, body = self._fetch(url, {"User-Agent": self._ua, "Accept": "application/json"})
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        if status in (401, 403):
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, f"http {status}")
        if status >= 500 or status == 429:
            return AcquisitionResult.failed(
                AcquisitionFailure.RATE_LIMITED if status == 429 else AcquisitionFailure.UNAVAILABLE, f"http {status}")
        hits = ((body or {}).get("hits") or {}).get("hits") or []
        if not hits:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, f"no EDGAR filer for {name!r}")
        # Collapse filing hits to distinct filers (one observation per CIK).
        seen: dict[str, dict] = {}
        for h in hits:
            src = h.get("_source", {}) or {}
            ciks = src.get("ciks") or ([src["cik"]] if src.get("cik") else [])
            for cik in ciks:
                cik = str(cik)
                if cik in seen:
                    continue
                names = src.get("display_names") or []
                seen[cik] = {
                    "cik": cik, "display_name": names[0] if names else "",
                    "tickers": src.get("tickers", ""), "latest_form": src.get("root_forms", src.get("form", "")),
                    "latest_filed": src.get("file_date", ""),
                }
        observations = list(seen.values())[:5]
        refs = [EvidenceRef(ref=f"sec_edgar:cik:{o['cik']}", source="sec_edgar",
                            content_hash=content_hash(o), ref_type="record") for o in observations]
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=Capability.COMPANY_IDENTITY,
            subject=name, observations=tuple(observations), source_refs=tuple(refs),
            retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), freshness_s=0.0,
            confidence=0.9 if len(observations) == 1 else 0.6, cost=0.0,
            license_scope="SEC EDGAR (US public domain)", raw_response_digest=content_hash(body),
            tenant=request.tenant)
        return AcquisitionResult.found((art,))
