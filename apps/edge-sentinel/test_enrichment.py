"""Phase 6 acceptance: a known fixture gives a reproducible graph; external claims carry source/time;
stale/revoked intel changes downstream status. Providers are contracts; tests use fixtures.
"""
from __future__ import annotations

import importlib

evidence = importlib.import_module("edge-sentinel.evidence")
cti = importlib.import_module("edge-sentinel.cti")
enr = importlib.import_module("edge-sentinel.enrichment")

Finding = evidence.Finding
FindingStatus = evidence.FindingStatus
CtiGraph = cti.CtiGraph
EnrichmentRecord = enr.EnrichmentRecord
is_stale = enr.is_stale
enrich = enr.enrich
reconcile_finding = enr.reconcile_finding
exposure_findings = enr.exposure_findings

NOW = "2026-09-21T12:00:00Z"


# fixture providers implement the real ABCs — no network
class FxDns(enr.DnsProvider):
    def resolve(self, domain):
        return EnrichmentRecord("fixture-dns", "8.8.8.8", domain, "dns",
                                {"addresses": ["203.0.113.7", "203.0.113.8"]}, retrieved_at=NOW)


class FxTI(enr.ThreatIntelProvider):
    def __init__(self, revoked=False, valid_until=""):
        self.revoked = revoked; self.valid_until = valid_until

    def reputation(self, ioc):
        return EnrichmentRecord("fixture-ti", "acme-ti", ioc, "threat-intel",
                                {"verdict": "malicious", "score": 0.9}, retrieved_at="2026-09-01T00:00:00Z",
                                valid_until=self.valid_until, revoked=self.revoked)


class FxExposure(enr.AssetExposureProvider):
    def exposed_services(self, asset):
        return EnrichmentRecord("fixture-shodan", "scan", asset, "exposure",
                                {"services": [{"port": 22, "service": "ssh"}, {"port": 3389, "service": "rdp"}]},
                                retrieved_at=NOW)


def test_providers_are_abstract_contracts():
    import pytest
    for abc in (enr.DnsProvider, enr.CertificateProvider, enr.RdapProvider,
                enr.ThreatIntelProvider, enr.AssetExposureProvider):
        with pytest.raises(TypeError):
            abc()


def test_enrichment_extends_the_graph_reproducibly():
    g1 = CtiGraph(); enrich(g1, "evil.example.com", now=NOW, dns=FxDns())
    g2 = CtiGraph(); enrich(g2, "evil.example.com", now=NOW, dns=FxDns())
    # same edges + ids (resolves-to the two IPs), regardless of run
    e1 = sorted((r.relationship_type, r.target_ref) for r in g1.relationships.values())
    e2 = sorted((r.relationship_type, r.target_ref) for r in g2.relationships.values())
    assert e1 == e2
    assert any(r.relationship_type == "resolves-to" for r in g1.relationships.values())


def test_external_claims_carry_source_and_time():
    g = CtiGraph()
    recs = enrich(g, "evil.example.com", now=NOW, dns=FxDns(), ti=FxTI(valid_until="2027-01-01T00:00:00Z"))
    for r in recs:
        assert r.provider and r.retrieved_at                 # every claim has source + time
    # and the graph edges carry the provider in their provenance
    for rel in g.relationships.values():
        assert rel.provenance.source and rel.provenance.created_at


def test_stale_and_revoked_intel():
    valid = FxTI(valid_until="2027-01-01T00:00:00Z").reputation("x")
    assert is_stale(valid, NOW) is False
    expired = FxTI(valid_until="2026-01-01T00:00:00Z").reputation("x")
    assert is_stale(expired, NOW) is True                    # past validity
    revoked = FxTI(revoked=True).reputation("x")
    assert is_stale(revoked, NOW) is True


def test_stale_intel_changes_downstream_finding_status():
    """A finding SUPPORTED by a threat-intel verdict drops to CONTRADICTED when that intel is revoked."""
    ti_rec = FxTI(revoked=True).reputation("203.0.113.7")
    finding = Finding(claim="203.0.113.7 is a known C2", confidence=0.9,
                      evidence_refs=(f"enrichment:{ti_rec.record_id}",), status=FindingStatus.SUPPORTED)
    reconciled = reconcile_finding(finding, ti_rec, NOW)
    assert reconciled.status is FindingStatus.CONTRADICTED
    assert any("stale-intel" in r for r in reconciled.contradicting_evidence_refs)
    # a still-valid verdict leaves the finding untouched
    fresh = FxTI(valid_until="2027-01-01T00:00:00Z").reputation("203.0.113.7")
    assert reconcile_finding(finding, fresh, NOW).status is FindingStatus.SUPPORTED


def test_stale_intel_adds_no_live_edge():
    g = CtiGraph()
    enrich(g, "203.0.113.7", now=NOW, ti=FxTI(revoked=True))
    assert not any(r.relationship_type == "enriched-by" for r in g.relationships.values())


def test_attack_surface_exposure_findings():
    g = CtiGraph()
    recs = enrich(g, "gateway.internal", now=NOW, exposure=FxExposure())
    findings = exposure_findings(recs, "gateway.internal")
    ports = {"22" in f.claim or "3389" in f.claim for f in findings}
    assert len(findings) == 2 and any("rdp" in f.claim for f in findings)
    assert any(r.relationship_type == "exposes" for r in g.relationships.values())
