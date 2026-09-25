"""WP-P1 tests — paid Bring-Your-Own intelligence adapters (Apollo, Similarweb, Semrush, Stripe·Radar,
Cloudflare TI, D&B, Brandwatch, VirusTotal, Defender TI). All offline via an injected `fetch`.

Pins: BYO entitlement (no credential ⇒ not entitled, no call), normalization to a governed EvidenceArtifact,
VirusTotal's commercial-license gate, Cloudflare's account-id requirement, and registry wiring.
"""
from __future__ import annotations

import pytest

from runtime_contracts.protocol import AcquisitionFailure, Capability, EvidenceRequest, IntelligenceRegistry

from agentic_os.intelligence import (
    ApolloProvider, BrandwatchProvider, CloudflareTiProvider, DefenderTiProvider, DnbProvider,
    SemrushProvider, SimilarwebProvider, StripeRadarProvider, VirusTotalProvider, acquire_for_decision,
    default_registry, register_paid_providers,
)


def _fetch(status, body):
    return lambda method, url, headers=None, body_=None: (status, body)


def _req(cap, subject, **over):
    base = dict(decision_case_id="dc", capability=cap, subject_refs=(subject,), purpose="p", tenant="t",
                max_cost=1.0)
    base.update(over)
    return EvidenceRequest(**base)


# ── BYO entitlement: no credential ⇒ not entitled + no call ─────────────────────────────────────────────
@pytest.mark.parametrize("provider,cap,subject", [
    (ApolloProvider(), Capability.PERSON_ENRICHMENT, "person:1"),
    (SimilarwebProvider(), Capability.WEB_TRAFFIC_INTELLIGENCE, "acme.com"),
    (SemrushProvider(), Capability.SEARCH_KEYWORD_INTELLIGENCE, "acme.com"),
    (StripeRadarProvider(), Capability.PAYMENT_FRAUD_SCORE, "ch_1"),
    (DnbProvider(), Capability.COMPANY_IDENTITY, "Acme"),
    (BrandwatchProvider(), Capability.SOCIAL_LISTENING, "acme"),
    (DefenderTiProvider(), Capability.THREAT_INTELLIGENCE, "evil.com"),
])
def test_paid_provider_not_entitled_without_credential(provider, cap, subject):
    assert not provider.check_entitlement("t", cap)
    assert provider.acquire(_req(cap, subject)).failure == AcquisitionFailure.NOT_ENTITLED


def test_apollo_enrichment_normalizes():
    p = ApolloProvider(credential="k", fetch=_fetch(200, {"person": {
        "id": "p1", "name": "Jane", "title": "CTO", "email": "j@acme.com", "email_status": "verified"}}))
    assert p.check_entitlement("t", Capability.PERSON_ENRICHMENT)
    res = p.acquire(_req(Capability.PERSON_ENRICHMENT, "Jane"))
    assert res.ok and res.artifacts[0].observations[0]["title"] == "CTO"
    assert res.artifacts[0].confidence == 0.85 and res.artifacts[0].has_provenance() and res.artifacts[0].cost == 0.05


def test_similarweb_and_stripe_and_dnb():
    sw = SimilarwebProvider(credential="k", fetch=_fetch(200, {"visits": [{"date": "2026-08", "visits": 1000}]}))
    assert sw.acquire(_req(Capability.WEB_TRAFFIC_INTELLIGENCE, "acme.com")).artifacts[0].observations[0]["visits"] == 1000
    st = StripeRadarProvider(credential="sk", fetch=_fetch(200, {"id": "ch_1", "outcome": {
        "risk_level": "elevated", "risk_score": 74, "network_status": "approved_by_network"}}))
    a = st.acquire(_req(Capability.PAYMENT_FRAUD_SCORE, "ch_1")).artifacts[0]
    assert a.observations[0]["risk_level"] == "elevated" and a.confidence == pytest.approx(0.74)
    dnb = DnbProvider(credential="tok", fetch=_fetch(200, {"matchCandidates": [
        {"organization": {"duns": "12", "primaryName": "Acme"}, "matchQualityInformation": {"confidenceCode": 8}}]}))
    assert dnb.acquire(_req(Capability.COMPANY_IDENTITY, "Acme")).artifacts[0].observations[0]["duns"] == "12"


def test_cloudflare_requires_account_id():
    no_acct = CloudflareTiProvider(credential="tok")
    assert not no_acct.check_entitlement("t", Capability.DOMAIN_REPUTATION)
    ok = CloudflareTiProvider(credential="tok", account_id="acct",
                              fetch=_fetch(200, {"result": {"domain": "evil.com", "risk_score": 90}}))
    assert ok.check_entitlement("t", Capability.DOMAIN_REPUTATION)
    a = ok.acquire(_req(Capability.DOMAIN_REPUTATION, "evil.com")).artifacts[0]
    assert a.observations[0]["risk_score"] == 90 and a.confidence == pytest.approx(0.9)


def test_virustotal_commercial_license_gate():
    free = VirusTotalProvider(credential="k", commercial=False, fetch=_fetch(200, {"data": {"attributes": {}}}))
    assert not free.check_entitlement("t", Capability.DOMAIN_REPUTATION)
    assert free.acquire(_req(Capability.DOMAIN_REPUTATION, "evil.com")).failure == AcquisitionFailure.LICENSE_BLOCKED
    comm = VirusTotalProvider(credential="k", commercial=True, fetch=_fetch(200, {"data": {"attributes": {
        "reputation": -40, "last_analysis_stats": {"malicious": 8, "harmless": 2}}}}))
    assert comm.check_entitlement("t", Capability.DOMAIN_REPUTATION)
    a = comm.acquire(_req(Capability.DOMAIN_REPUTATION, "evil.com")).artifacts[0]
    assert a.observations[0]["malicious"] == 8


def test_http_failures_map_to_taxonomy():
    p = ApolloProvider(credential="k", fetch=_fetch(429, {}))
    assert p.acquire(_req(Capability.PERSON_ENRICHMENT, "x")).failure == AcquisitionFailure.RATE_LIMITED
    p2 = ApolloProvider(credential="k", fetch=_fetch(503, {}))
    assert p2.acquire(_req(Capability.PERSON_ENRICHMENT, "x")).failure == AcquisitionFailure.UNAVAILABLE
    p3 = ApolloProvider(credential="k", fetch=_fetch(401, {}))
    assert p3.acquire(_req(Capability.PERSON_ENRICHMENT, "x")).failure == AcquisitionFailure.NOT_ENTITLED


def test_registry_wiring_with_paid_providers():
    reg = default_registry()                       # open baseline
    register_paid_providers(reg, apollo_key="k", virustotal_key="k", virustotal_commercial=True)
    # a person-enrichment request now routes to Apollo (the only entitled provider for that capability)
    cands = reg.match(_req(Capability.PERSON_ENRICHMENT, "Jane"))
    assert [c.provider_id for c in cands] == ["apollo"]
    # blank credentials never register (open stack unaffected)
    reg2 = default_registry()
    register_paid_providers(reg2)  # nothing supplied
    assert reg2.match(_req(Capability.PERSON_ENRICHMENT, "Jane")) == ()
