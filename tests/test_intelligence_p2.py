"""P2 customer-driven enterprise adapters (moat §3.10/§3.12, §6 P2): entity/risk + vuln scanners.

All BYO and offline via injected fetch. Verifies entitlement (no key ⇒ not registered), capability mapping,
screening clean-hit-as-evidence, exploitability confidence, and on-demand registration leaving the baseline intact.
"""
from __future__ import annotations

from runtime_contracts.protocol import Capability, EvidenceRequest

from agentic_os.intelligence import (
    LexisNexisRiskProvider, LsegRiskProvider, MoodysProvider, QualysProvider, Rapid7Provider, TenableProvider,
    ZoomInfoProvider, default_registry, register_p2_providers,
)


def _fetch(status, body):
    return lambda method, url, headers=None, body_=None: (status, body)


def _req(cap, subject="acme", tenant="t", jurisdiction=""):
    return EvidenceRequest(decision_case_id="dc", capability=cap, subject_refs=(subject,),
                           purpose="diligence", tenant=tenant, jurisdiction=jurisdiction, max_cost=1.0)


# ── entity / risk ─────────────────────────────────────────────────────────────────────────────────────────
def test_zoominfo_enriches_contact():
    p = ZoomInfoProvider(credential="k", fetch=_fetch(200, {"data": {"result": [
        {"data": [{"id": "9", "firstName": "Jane", "email": "jane@acme.com", "jobTitle": "CFO"}]}]}}))
    res = p.acquire(_req(Capability.PERSON_ENRICHMENT, "Jane Doe"))
    assert res.ok and res.artifacts[0].observations[0]["email"] == "jane@acme.com"
    assert res.artifacts[0].confidence >= 0.85


def test_zoominfo_is_byo():
    assert not ZoomInfoProvider().check_entitlement("t", Capability.PERSON_ENRICHMENT)


def test_lseg_clean_screen_is_evidence_not_failure():
    p = LsegRiskProvider(credential="k", fetch=_fetch(200, {"results": []}))
    res = p.acquire(_req(Capability.SANCTIONS_RISK, "Clean Corp"))
    assert res.ok                                   # clean screen returns evidence
    assert res.artifacts[0].observations[0] == {"match": False, "hits": 0}
    assert res.artifacts[0].confidence == 0.9


def test_lseg_hit_is_flagged_with_score():
    p = LsegRiskProvider(credential="k", fetch=_fetch(200, {"results": [
        {"id": "wc1", "name": "Bad Actor", "categories": ["SANCTIONS"], "score": 0.92}]}))
    res = p.acquire(_req(Capability.SANCTIONS_RISK, "Bad Actor"))
    assert res.ok and res.artifacts[0].observations[0] == {"match": True, "hits": 1}
    assert res.artifacts[0].source_refs and res.artifacts[0].confidence >= 0.9


def test_lexisnexis_risk_distinct_id_and_caps():
    p = LexisNexisRiskProvider(credential="k", fetch=_fetch(200, {"hits": []}))
    assert p.provider_id == "lexisnexis_risk"
    assert p.check_entitlement("t", Capability.BENEFICIAL_OWNERSHIP)
    assert p.acquire(_req(Capability.SANCTIONS_RISK, "X")).ok


def test_moodys_maps_orbis_hierarchy():
    p = MoodysProvider(credential="k", fetch=_fetch(200, {"matchedCompanies": [
        {"bvdId": "US123", "name": "Acme Inc", "ultimateOwner": "Acme Holdings", "matchScore": 0.88}]}))
    res = p.acquire(_req(Capability.CORPORATE_HIERARCHY, "Acme"))
    assert res.ok
    hit = res.artifacts[0].observations[1]
    assert hit["ultimate_owner"] == "Acme Holdings" and hit["id"] == "US123"


# ── vuln scanners ─────────────────────────────────────────────────────────────────────────────────────────
def test_tenable_confidence_tracks_vpr():
    p = TenableProvider(credential="accessKey=a;secretKey=s", fetch=_fetch(200, {"vulnerabilities": [
        {"plugin_id": 1, "plugin_name": "OpenSSL", "severity": "critical", "vpr_score": 9.1, "cve": "CVE-1"}]}))
    res = p.acquire(_req(Capability.VULNERABILITY_EXPLOITABILITY, "10.0.0.5"))
    assert res.ok and res.artifacts[0].confidence >= 0.9
    assert res.artifacts[0].observations[0]["cve"] == "CVE-1"


def test_qualys_normalized_detections():
    p = QualysProvider(credential="dG9r", fetch=_fetch(200, {"detections": [
        {"qid": 105, "title": "Weak TLS", "severity": "medium", "cve_id": "CVE-2"}]}))
    res = p.acquire(_req(Capability.VULNERABILITY_EXPLOITABILITY, "10.0.0.9"))
    assert res.ok and res.artifacts[0].observations[0]["qid"] == 105


def test_rapid7_supports_threat_intel_cap():
    p = Rapid7Provider(credential="k")
    assert Capability.THREAT_INTELLIGENCE in p.capabilities()
    p2 = Rapid7Provider(credential="k", fetch=_fetch(200, {"data": [
        {"id": "v1", "title": "RCE", "severity": "Critical", "riskScore": 8.7, "exploits": 2}]}))
    res = p2.acquire(_req(Capability.VULNERABILITY_EXPLOITABILITY, "host-1"))
    assert res.ok and res.artifacts[0].confidence >= 0.8


# ── on-demand registration ────────────────────────────────────────────────────────────────────────────────
def test_register_p2_only_wires_supplied_credentials():
    reg = default_registry()
    register_p2_providers(reg, tenable_key="tk", moodys_token="mt")   # only 2 supplied
    # both entitled capabilities now resolve to a provider
    assert reg.match(_req(Capability.VULNERABILITY_EXPLOITABILITY))
    assert reg.match(_req(Capability.CORPORATE_HIERARCHY))
    # a P2 provider whose credential wasn't supplied is simply not registered
    assert reg.get("zoominfo") is None and reg.get("qualys") is None
    assert reg.get("tenable") is not None and reg.get("moodys") is not None
