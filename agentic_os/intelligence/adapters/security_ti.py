"""Security threat-intelligence BYO adapters: Cloudflare TI, VirusTotal (commercial-only), Defender TI (moat §3.9/3.10).

Global telemetry that local sensors (CrowdSec/OpenSCAP) can't reproduce. All BYO. VirusTotal enforces a commercial
license flag — the plan is explicit that the free public API must not back a commercial product (§3.9). Threat
feeds inform governed action; they never create authority, and a destructive action must not rely on one score.
"""
from __future__ import annotations

from urllib.parse import quote

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, EvidenceRef, EvidenceRequest, ProviderFamily, content_hash,
)

from ._base import HttpEvidenceProvider
from ._http import Fetch, http_json


class CloudflareTiProvider(HttpEvidenceProvider):
    """Cloudflare Security Center — domain/IP intelligence, passive DNS, phishing/threat events (§3.9 P0)."""
    provider_id = "cloudflare_ti"
    _caps = (Capability.DOMAIN_REPUTATION, Capability.IP_REPUTATION, Capability.PASSIVE_DNS,
             Capability.THREAT_INTELLIGENCE)
    _price = 0.0
    _license = "Cloudflare Security Center (licensed)"
    _BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self, credential: str = "", account_id: str = "", fetch: Fetch = http_json):
        super().__init__(credential, fetch)
        self._account = account_id

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return super().check_entitlement(tenant, capability) and bool(self._account)

    def _request(self, request):
        subject = request.subject_refs[0]
        url = f"{self._BASE}/accounts/{quote(self._account)}/intel/domain?domain={quote(subject)}"
        return "GET", url, {"Authorization": f"Bearer {self._cred}"}, None

    def _extract(self, body, request):
        res = body.get("result") or {}
        if not res:
            return [], [], 0.0
        obs = [{k: res.get(k) for k in ("domain", "risk_score", "application", "content_categories",
                                        "resolves_to_refs") if k in res}]
        score = res.get("risk_score")
        conf = min(0.99, score / 100.0) if isinstance(score, (int, float)) else 0.7
        return obs, [EvidenceRef(ref=f"cloudflare:domain:{res.get('domain','')}", source="cloudflare_ti",
                                 content_hash=content_hash(body), ref_type="record")], conf


class VirusTotalProvider(HttpEvidenceProvider):
    """VirusTotal — malware/domain/IP/URL reputation (§3.9 P0/P1). BYO key AND explicit commercial license.

    The free/public API is NOT permitted as a commercial-product dependency — this adapter refuses to run without
    `commercial=True` even if a key is present.
    """
    provider_id = "virustotal"
    _caps = (Capability.MALWARE_REPUTATION, Capability.DOMAIN_REPUTATION, Capability.IP_REPUTATION)
    _price = 0.0
    _license = "VirusTotal (COMMERCIAL/private API only)"
    _BASE = "https://www.virustotal.com/api/v3"

    def __init__(self, credential: str = "", commercial: bool = False, fetch: Fetch = http_json):
        super().__init__(credential, fetch)
        self._commercial = commercial

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        # gate on a commercial license, not just a key — the free public API can't back a commercial product.
        return super().check_entitlement(tenant, capability) and self._commercial

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        if self._cred and not self._commercial:
            return AcquisitionResult.failed(
                AcquisitionFailure.LICENSE_BLOCKED, "VirusTotal free/public API not permitted for commercial use")
        return super().acquire(request)

    def _request(self, request):
        subject = request.subject_refs[0]
        kind = {Capability.IP_REPUTATION: "ip_addresses", Capability.DOMAIN_REPUTATION: "domains",
                Capability.MALWARE_REPUTATION: "files"}.get(request.capability, "domains")
        return "GET", f"{self._BASE}/{kind}/{quote(subject)}", {"x-apikey": self._cred}, None

    def _extract(self, body, request):
        attr = ((body.get("data") or {}).get("attributes")) or {}
        if not attr:
            return [], [], 0.0
        stats = attr.get("last_analysis_stats", {}) or {}
        obs = [{"reputation": attr.get("reputation"), "last_analysis_stats": stats,
                "malicious": stats.get("malicious", 0), "suspicious": stats.get("suspicious", 0)}]
        total = sum(stats.values()) or 1
        conf = min(0.99, (stats.get("malicious", 0) + stats.get("suspicious", 0)) / total) if total else 0.5
        return obs, [EvidenceRef(ref=f"virustotal:{request.subject_refs[0]}", source="virustotal",
                                 content_hash=content_hash(body), ref_type="record")], conf


class DefenderTiProvider(HttpEvidenceProvider):
    """Microsoft Defender Threat Intelligence via Graph — finished intel, IoCs, passive DNS (§3.9 P1). BYO Graph token."""
    provider_id = "defender_ti"
    _caps = (Capability.THREAT_INTELLIGENCE, Capability.PASSIVE_DNS)
    _price = 0.0
    _license = "Microsoft Defender TI (customer-licensed)"
    _BASE = "https://graph.microsoft.com/beta/security/threatIntelligence"

    def _request(self, request):
        host = request.subject_refs[0]
        return "GET", f"{self._BASE}/hosts/{quote(host)}", {"Authorization": f"Bearer {self._cred}"}, None

    def _extract(self, body, request):
        # a single host resource, or a collection under `value`.
        recs = body.get("value") if isinstance(body.get("value"), list) else ([body] if body.get("id") else [])
        if not recs:
            return [], [], 0.0
        obs = [{k: r.get(k) for k in ("id", "registrar", "firstSeenDateTime", "reputation") if k in r}
               for r in recs[:5]]
        return obs, [EvidenceRef(ref=f"defender:host:{request.subject_refs[0]}", source="defender_ti",
                                 content_hash=content_hash(body), ref_type="record")], 0.7
