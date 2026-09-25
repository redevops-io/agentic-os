"""Shared base for Bring-Your-Own-credential intelligence adapters (moat plan §12 P1 providers).

Every paid provider (Apollo, Similarweb, Semrush, Stripe·Radar, Cloudflare TI, D&B, Brandwatch, VirusTotal,
Defender TI) follows the same shape: entitled only when the tenant supplies a credential, one HTTP call, a
normalized governed EvidenceArtifact with provenance/cost/freshness/license, and HTTP/errors mapped onto the
AcquisitionFailure taxonomy. Subclasses implement just `_request` (method/url/headers/body) and `_extract`
(observations/refs/confidence). Fully offline-testable via the injected `fetch`.
"""
from __future__ import annotations

import time
from typing import Optional

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, EvidenceArtifact, EvidenceRef,
    EvidenceRequest, ProviderFamily, content_hash,
)

from ._http import Fetch, http_json


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def status_failure(status: int) -> Optional[AcquisitionFailure]:
    if status in (401, 403):
        return AcquisitionFailure.NOT_ENTITLED
    if status == 402:
        return AcquisitionFailure.LICENSE_BLOCKED     # payment required
    if status == 429:
        return AcquisitionFailure.RATE_LIMITED
    if status >= 500:
        return AcquisitionFailure.UNAVAILABLE
    return None


class HttpEvidenceProvider:
    """Base BYO provider. Subclasses set provider_id/family/_caps/_price/_license and implement _request/_extract."""
    provider_id: str = "?"
    family: ProviderFamily = ProviderFamily.EXTERNAL_DATA
    _caps: tuple[Capability, ...] = ()
    _price: float = 0.0
    _license: str = ""

    def __init__(self, credential: str = "", fetch: Fetch = http_json):
        self._cred = credential
        self._fetch = fetch

    def capabilities(self) -> tuple[Capability, ...]:
        return self._caps

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=self._price, latency_ms=500)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in self._caps and bool(self._cred)   # BYO: no credential ⇒ not entitled

    # ── subclass hooks ─────────────────────────────────────────────────────────────────────────────────
    def _request(self, request: EvidenceRequest) -> "tuple[str, str, dict, Optional[dict]]":
        """Return (method, url, headers, body) for the provider call."""
        raise NotImplementedError

    def _extract(self, body: dict, request: EvidenceRequest) -> "tuple[list[dict], list[EvidenceRef], float]":
        """Return (observations, source_refs, confidence) from the parsed response."""
        raise NotImplementedError

    def _absence_is_evidence(self) -> bool:
        """Screening providers (e.g. sanctions/fraud) may treat 'no hit' as evidence, not a NO_MATCH failure."""
        return False

    # ── template acquire ───────────────────────────────────────────────────────────────────────────────
    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        subject = (request.subject_refs or ("",))[0]
        if not subject:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        if not self._cred:
            return AcquisitionResult.failed(AcquisitionFailure.NOT_ENTITLED, f"no {self.provider_id} credential")
        method, url, headers, body = self._request(request)
        try:
            status, resp = self._fetch(method, url, headers, body)
        except Exception as e:  # noqa: BLE001
            return AcquisitionResult.failed(AcquisitionFailure.UNAVAILABLE, str(e))
        fail = status_failure(status)
        if fail:
            return AcquisitionResult.failed(fail, f"http {status}")
        obs, refs, confidence = self._extract(resp or {}, request)
        if not obs and not self._absence_is_evidence():
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, f"no result for {subject!r}")
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=request.capability, subject=subject,
            observations=tuple(obs), source_refs=tuple(refs), retrieved_at=_now(), freshness_s=0.0,
            confidence=confidence, cost=self._price, license_scope=self._license,
            raw_response_digest=content_hash(resp), tenant=request.tenant)
        return AcquisitionResult.found((art,), cost=self._price)
