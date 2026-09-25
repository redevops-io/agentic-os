"""GTM / market / entity BYO adapters: Apollo, Similarweb, Semrush, D&B Direct+, Brandwatch (moat §3.1/3.5-3.8/3.12).

All Bring-Your-Own-credential (§12): entitled only when the tenant supplies a key, so the open stack runs without
them. Each maps its documented JSON response onto a governed EvidenceArtifact via the shared base.
"""
from __future__ import annotations

from urllib.parse import quote

from runtime_contracts.protocol import Capability, EvidenceRef, ProviderFamily, content_hash

from ._base import HttpEvidenceProvider


class ApolloProvider(HttpEvidenceProvider):
    """Apollo — people/company graph + verified contact enrichment (§3.1 P0). BYO api key."""
    provider_id = "apollo"
    family = ProviderFamily.EXTERNAL_DATA
    _caps = (Capability.PERSON_ENRICHMENT, Capability.PERSON_SEARCH, Capability.COMPANY_ENRICHMENT)
    _price = 0.05
    _license = "Apollo (enrichment; per-tenant, no training)"
    _BASE = "https://api.apollo.io/api/v1"

    def _request(self, request):
        subject = request.subject_refs[0]
        ep = "organizations/enrich" if request.capability == Capability.COMPANY_ENRICHMENT else "people/match"
        return "POST", f"{self._BASE}/{ep}", {"x-api-key": self._cred}, {"name": subject}

    def _extract(self, body, request):
        rec = body.get("organization") or body.get("person") or {}
        if not rec:
            return [], [], 0.0
        obs = [{k: rec.get(k) for k in ("id", "name", "title", "email", "email_status", "linkedin_url",
                                        "organization_name", "estimated_num_employees") if k in rec}]
        conf = 0.85 if rec.get("email_status") in ("verified", "likely to engage") else 0.6
        return obs, [EvidenceRef(ref=f"apollo:{rec.get('id','')}", source="apollo",
                                 content_hash=content_hash(rec), ref_type="record")], conf


class SimilarwebProvider(HttpEvidenceProvider):
    """Similarweb — external web/market/competitor traffic intelligence (§3.7 P0). BYO api key."""
    provider_id = "similarweb"
    _caps = (Capability.WEB_TRAFFIC_INTELLIGENCE,)
    _price = 0.03
    _license = "Similarweb (licensed market data)"
    _BASE = "https://api.similarweb.com/v1"

    def _request(self, request):
        domain = request.subject_refs[0]
        url = (f"{self._BASE}/website/{quote(domain)}/total-traffic-and-engagement/visits"
               f"?api_key={quote(self._cred)}&granularity=monthly&main_domain_only=true")
        return "GET", url, {}, None

    def _extract(self, body, request):
        visits = body.get("visits") or []
        if not visits:
            return [], [], 0.0
        obs = [{"date": v.get("date"), "visits": v.get("visits")} for v in visits[-6:]]
        return obs, [EvidenceRef(ref=f"similarweb:{request.subject_refs[0]}", source="similarweb",
                                 content_hash=content_hash(body), ref_type="metric")], 0.8


class SemrushProvider(HttpEvidenceProvider):
    """Semrush — keyword/backlink/competitive search intelligence (§3.7/3.8 P0/P1). BYO api key.

    NOTE: the live Semrush Analytics API returns CSV; the real transport converts rows→dicts before _extract.
    """
    provider_id = "semrush"
    _caps = (Capability.SEARCH_KEYWORD_INTELLIGENCE, Capability.BACKLINK_INTELLIGENCE)
    _price = 0.04
    _license = "Semrush (licensed search data)"
    _BASE = "https://api.semrush.com/analytics/v1"

    def _request(self, request):
        domain = request.subject_refs[0]
        rtype = "backlinks_overview" if request.capability == Capability.BACKLINK_INTELLIGENCE else "domain_ranks"
        url = f"{self._BASE}/?key={quote(self._cred)}&type={rtype}&target={quote(domain)}&target_type=root_domain"
        return "GET", url, {}, None

    def _extract(self, body, request):
        rows = body.get("data") or []
        if not rows:
            return [], [], 0.0
        return list(rows[:10]), [EvidenceRef(ref=f"semrush:{request.subject_refs[0]}", source="semrush",
                                             content_hash=content_hash(body), ref_type="metric")], 0.8


class DnbProvider(HttpEvidenceProvider):
    """D&B Direct+ — canonical business identity / D-U-N-S / hierarchy / risk (§3.1/3.2 P1). BYO bearer token."""
    provider_id = "dnb"
    _caps = (Capability.COMPANY_IDENTITY, Capability.COMPANY_ENRICHMENT, Capability.CORPORATE_HIERARCHY)
    _price = 0.20
    _license = "D&B Direct+ (commercial)"
    _BASE = "https://plus.dnb.com"

    def _request(self, request):
        name = request.subject_refs[0]
        url = f"{self._BASE}/v1/match/cleanseMatch?name={quote(name)}"
        if request.jurisdiction:
            url += f"&countryISOAlpha2Code={quote(request.jurisdiction)}"
        return "GET", url, {"Authorization": f"Bearer {self._cred}"}, None

    def _extract(self, body, request):
        cands = body.get("matchCandidates") or []
        if not cands:
            return [], [], 0.0
        obs, refs = [], []
        for c in cands[:5]:
            org = c.get("organization", {}) or {}
            obs.append({"duns": org.get("duns"), "name": org.get("primaryName"),
                        "confidence_code": c.get("matchQualityInformation", {}).get("confidenceCode"),
                        "country": (org.get("primaryAddress", {}) or {}).get("addressCountry", {}).get("isoAlpha2Code")})
            refs.append(EvidenceRef(ref=f"dnb:duns:{org.get('duns','')}", source="dnb",
                                    content_hash=content_hash(c), ref_type="record"))
        return obs, refs, 0.9 if len(cands) == 1 else 0.6


class BrandwatchProvider(HttpEvidenceProvider):
    """Brandwatch — licensed social/consumer listening (§3.6 P0/P1). BYO access token."""
    provider_id = "brandwatch"
    _caps = (Capability.SOCIAL_LISTENING,)
    _price = 0.10
    _license = "Brandwatch (licensed social data)"
    _BASE = "https://api.brandwatch.com"

    def _request(self, request):
        query = request.subject_refs[0]
        url = f"{self._BASE}/query/summary?queryString={quote(query)}&access_token={quote(self._cred)}"
        return "GET", url, {}, None

    def _extract(self, body, request):
        results = body.get("results") or []
        if not results:
            return [], [], 0.0
        obs = [{"topic": r.get("topic"), "volume": r.get("volume"), "sentiment": r.get("sentiment")}
               for r in results[:10]]
        return obs, [EvidenceRef(ref=f"brandwatch:{request.subject_refs[0]}", source="brandwatch",
                                 content_hash=content_hash(body), ref_type="metric")], 0.75
