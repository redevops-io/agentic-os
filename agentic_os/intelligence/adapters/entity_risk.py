"""P2 customer-driven entity / risk BYO adapters: ZoomInfo, LSEG Risk Intelligence, LexisNexis Risk Solutions,
Moody's (moat plan §3.12, §6 P2).

These are enterprise, licensed, per-customer providers — built to be *ready to wire* but registered only when a
pilot/customer supplies credentials (§6 P2: "add when a pilot/customer creates a concrete evidence requirement").
They reuse the existing entity/risk capabilities, so no contract change is needed. Screening providers treat a
clean "no hit" as decision-relevant evidence (§ like OpenSanctions), not a NO_MATCH failure.

Note: LexisNexis Risk Solutions is distinct from the LexisNexis *legal* provider in `agentic_os/legal/providers.py`
— different product, different capabilities, hence `provider_id = "lexisnexis_risk"`.
"""
from __future__ import annotations

from urllib.parse import quote

from runtime_contracts.protocol import Capability, EvidenceRef, ProviderFamily, content_hash

from ._base import HttpEvidenceProvider


class ZoomInfoProvider(HttpEvidenceProvider):
    """ZoomInfo — B2B people/company graph + contact enrichment (§3.1/3.11). BYO bearer token."""
    provider_id = "zoominfo"
    _caps = (Capability.PERSON_ENRICHMENT, Capability.PERSON_SEARCH, Capability.COMPANY_ENRICHMENT)
    _price = 0.12
    _license = "ZoomInfo (licensed; per-tenant, no training)"
    _BASE = "https://api.zoominfo.com"

    def _request(self, request):
        subject = request.subject_refs[0]
        if request.capability == Capability.COMPANY_ENRICHMENT:
            ep, key = "enrich/company", "companyName"
        else:
            ep, key = "enrich/contact", "fullName"
        return "POST", f"{self._BASE}/{ep}", {"Authorization": f"Bearer {self._cred}"}, {
            "matchPersonInput" if key == "fullName" else "matchCompanyInput": [{key: subject}],
            "outputFields": ["id", "firstName", "lastName", "email", "jobTitle", "companyName", "employeeCount"],
        }

    def _extract(self, body, request):
        results = ((body.get("data") or {}).get("result")) or body.get("result") or []
        recs = []
        for r in results[:5]:
            data = r.get("data") or r
            recs.extend(data if isinstance(data, list) else [data])
        recs = [r for r in recs if r]
        if not recs:
            return [], [], 0.0
        obs = [{k: r.get(k) for k in ("id", "firstName", "lastName", "email", "jobTitle", "companyName",
                                      "employeeCount") if k in r} for r in recs[:5]]
        conf = 0.85 if any(r.get("email") for r in recs) else 0.6
        return obs, [EvidenceRef(ref=f"zoominfo:{recs[0].get('id','')}", source="zoominfo",
                                 content_hash=content_hash(body), ref_type="record")], conf


class _ScreeningProvider(HttpEvidenceProvider):
    """Base for risk/screening providers where a clean screen is evidence. Subclasses implement `_request` and
    `_hits(body)` → list of matched-entity dicts; a clean result becomes a {'match': False} observation."""
    family = ProviderFamily.EXTERNAL_DATA

    def _hits(self, body: dict, request) -> list[dict]:
        raise NotImplementedError

    def _absence_is_evidence(self) -> bool:
        return True

    def _extract(self, body, request):
        hits = self._hits(body or {}, request)
        if not hits:
            # screened, nothing found — decision-relevant, high-confidence clean evidence.
            return [{"match": False, "hits": 0}], [], 0.9
        obs = [{"match": True, "hits": len(hits)}]
        refs = []
        top = 0.0
        for h in hits[:5]:
            score = float(h.get("score", h.get("matchStrength", 0.0)) or 0.0)
            top = max(top, score)
            obs.append(h)
            refs.append(EvidenceRef(ref=f"{self.provider_id}:{h.get('id','')}", source=self.provider_id,
                                    content_hash=content_hash(h), ref_type="record"))
        return obs, refs, min(0.99, top) if top else 0.8


class LsegRiskProvider(_ScreeningProvider):
    """LSEG Risk Intelligence (World-Check One) — sanctions / PEP / adverse-media + entity/ownership (§3.12 P2)."""
    provider_id = "lseg_risk"
    _caps = (Capability.SANCTIONS_RISK, Capability.BENEFICIAL_OWNERSHIP, Capability.COMPANY_IDENTITY)
    _price = 0.30
    _license = "LSEG Risk Intelligence (World-Check, licensed)"
    _BASE = "https://api.risk.lseg.com/v2"

    def _request(self, request):
        name = request.subject_refs[0]
        entity = "ORGANISATION" if request.capability != Capability.SANCTIONS_RISK else "UNSPECIFIED"
        return "POST", f"{self._BASE}/cases/screeningRequest", {"Authorization": f"Bearer {self._cred}"}, {
            "name": name, "entityType": entity, "providerTypes": ["WATCHLIST", "MEDIA_CHECK"],
            "jurisdiction": request.jurisdiction or None}

    def _hits(self, body, request):
        return body.get("results") or body.get("matches") or []


class LexisNexisRiskProvider(_ScreeningProvider):
    """LexisNexis Risk Solutions — identity verification + sanctions/PEP + beneficial ownership (§3.12 P2).

    Distinct from the LexisNexis legal-research provider; different product and capabilities."""
    provider_id = "lexisnexis_risk"
    _caps = (Capability.SANCTIONS_RISK, Capability.PERSON_ENRICHMENT, Capability.BENEFICIAL_OWNERSHIP)
    _price = 0.25
    _license = "LexisNexis Risk Solutions (licensed)"
    _BASE = "https://risk.api.lexisnexis.com/v1"

    def _request(self, request):
        name = request.subject_refs[0]
        return "POST", f"{self._BASE}/screening", {"Authorization": f"Bearer {self._cred}"}, {
            "subject": name, "checks": ["sanctions", "pep", "adverse_media"],
            "country": request.jurisdiction or None}

    def _hits(self, body, request):
        return body.get("hits") or (body.get("result") or {}).get("matches") or []


class MoodysProvider(_ScreeningProvider):
    """Moody's (Orbis / BvD) — company identity, corporate hierarchy, beneficial ownership, commercial risk (§3.12 P2)."""
    provider_id = "moodys"
    _caps = (Capability.COMPANY_IDENTITY, Capability.CORPORATE_HIERARCHY, Capability.BENEFICIAL_OWNERSHIP)
    _price = 0.35
    _license = "Moody's / Bureau van Dijk Orbis (licensed)"
    _BASE = "https://api.moodys.com/orbis/v1"

    def _request(self, request):
        name = request.subject_refs[0]
        return "POST", f"{self._BASE}/companies/match", {"Authorization": f"Bearer {self._cred}"}, {
            "name": name, "country": request.jurisdiction or None,
            "fields": ["bvdId", "name", "country", "ultimateOwner", "corporateGroup"]}

    def _hits(self, body, request):
        cands = body.get("matchedCompanies") or body.get("companies") or []
        # normalize Orbis fields into the screening hit shape
        out = []
        for c in cands:
            out.append({"id": c.get("bvdId", c.get("id", "")), "name": c.get("name"),
                        "country": c.get("country"), "ultimate_owner": c.get("ultimateOwner"),
                        "corporate_group": c.get("corporateGroup"),
                        "score": c.get("matchScore", c.get("score", 0.0))})
        return out
