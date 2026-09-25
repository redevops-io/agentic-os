"""Specialist legal Professional-Intelligence providers (moat plan §19.2). BYO credentials.

LexisNexis and Thomson Reuters CoCounsel expose programmatic legal research/review grounded in authoritative
sources. These adapters preserve authority/jurisdiction/citations (§19.6) — a generated proposition with no
authority is NOT authoritative (§19.7). They register in the same IntelligenceRegistry as any provider; legal
GOVERNANCE (what action the evidence justifies) is enforced separately in governance.py.
"""
from __future__ import annotations

from urllib.parse import quote

from runtime_contracts.protocol import (
    Capability, EvidenceArtifact, EvidenceRef, LegalEvidenceArtifact, ProviderFamily, content_hash,
)

from ..intelligence.adapters._base import HttpEvidenceProvider


class _LegalProvider(HttpEvidenceProvider):
    """Base for specialist legal providers: family=professional_intelligence; stashes authority metadata in the
    observation so `legal_artifact_from` can rebuild the typed LegalEvidenceArtifact."""
    family = ProviderFamily.PROFESSIONAL_INTELLIGENCE

    def _authority(self, body: dict, request) -> dict:
        raise NotImplementedError

    def _extract(self, body, request):
        auth = self._authority(body, request)
        if not auth.get("authority_refs") and not auth.get("summary"):
            return [], [], 0.0
        refs = [EvidenceRef(ref=f"{self.provider_id}:{a}", source=self.provider_id,
                            content_hash=content_hash(a), ref_type="authority")
                for a in auth.get("authority_refs", [])]
        return [auth], refs, float(auth.get("provider_confidence", 0.7))


def legal_artifact_from(evidence: EvidenceArtifact) -> LegalEvidenceArtifact:
    """Rebuild the typed LegalEvidenceArtifact from a legal provider's EvidenceArtifact (§19.6)."""
    a = evidence.observations[0] if evidence.observations else {}
    return LegalEvidenceArtifact(
        artifact=evidence, jurisdiction=a.get("jurisdiction", ""),
        authority_type=a.get("authority_type", ""),
        authority_refs=tuple(a.get("authority_refs", ())), cited_passages=tuple(a.get("cited_passages", ())),
        known_at=a.get("known_at", ""), source_status=a.get("source_status", ""),
        provider_confidence=float(a.get("provider_confidence", evidence.confidence)))


class LexisNexisProvider(_LegalProvider):
    """LexisNexis / Lexis+ API — jurisdiction-aware research grounded in Primary Law + Secondary Materials."""
    provider_id = "lexisnexis"
    _caps = (Capability.LEGAL_RESEARCH, Capability.LEGAL_AUTHORITY_LOOKUP, Capability.LEGAL_CLAUSE_REVIEW)
    _price = 0.50
    _license = "LexisNexis (licensed legal content)"
    _BASE = "https://api.lexisnexis.com/v1"

    def _request(self, request):
        q = request.subject_refs[0]
        url = f"{self._BASE}/research?query={quote(q)}"
        if request.jurisdiction:
            url += f"&jurisdiction={quote(request.jurisdiction)}"
        return "GET", url, {"Authorization": f"Bearer {self._cred}"}, None

    def _authority(self, body, request):
        r = body.get("result") or {}
        return {
            "summary": r.get("summary", ""),
            "jurisdiction": r.get("jurisdiction", request.jurisdiction),
            "authority_type": r.get("authority_type", "primary_law"),
            "authority_refs": r.get("citations", []),
            "cited_passages": r.get("passages", []),
            "known_at": r.get("as_of", ""),
            "source_status": r.get("status", "unknown"),
            "provider_confidence": r.get("confidence", 0.75),
        }


class CoCounselProvider(_LegalProvider):
    """Thomson Reuters CoCounsel — legal AI over Westlaw/Practical Law: research, review, comparison."""
    provider_id = "cocounsel"
    _caps = (Capability.LEGAL_RESEARCH, Capability.LEGAL_DOCUMENT_REVIEW, Capability.LEGAL_PRECEDENT_LOOKUP)
    _price = 0.60
    _license = "Thomson Reuters CoCounsel (licensed)"
    _BASE = "https://api.cocounsel.thomsonreuters.com/v1"

    def _request(self, request):
        q = request.subject_refs[0]
        return "POST", f"{self._BASE}/skills/research", {"Authorization": f"Bearer {self._cred}"}, {
            "query": q, "jurisdiction": request.jurisdiction}

    def _authority(self, body, request):
        r = body.get("response") or {}
        return {
            "summary": r.get("answer", ""),
            "jurisdiction": r.get("jurisdiction", request.jurisdiction),
            "authority_type": r.get("authority_type", "secondary"),
            "authority_refs": [c.get("cite") for c in r.get("authorities", []) if c.get("cite")],
            "cited_passages": [c.get("passage") for c in r.get("authorities", []) if c.get("passage")],
            "known_at": r.get("as_of", ""),
            "source_status": r.get("status", "unknown"),
            "provider_confidence": r.get("confidence", 0.7),
        }
