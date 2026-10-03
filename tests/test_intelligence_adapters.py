"""WP2 tests — open intelligence adapters + Discovery bridge + evidence-value accounting.

All offline: adapters take an injected `fetch` returning canned (status, body), so no test hits the network.
Pins: normalization to a governed EvidenceArtifact (provenance/cost/freshness), open-vs-BYO entitlement, the
failure taxonomy, the Discovery bridge gate, and the value ledger.
"""
from __future__ import annotations

import tempfile

from runtime_contracts.protocol import (
    AcquisitionFailure, Capability, EvidenceRequest, IntelligenceRegistry,
)

from agentic_os.intelligence import EvidenceValueStore, acquire_for_decision, default_registry
from agentic_os.intelligence.adapters.gleif import GleifProvider
from agentic_os.intelligence.adapters.opencorporates import OpenCorporatesProvider
from agentic_os.intelligence.adapters.opensanctions import OpenSanctionsProvider


def _fetch(status, body):
    return lambda url, headers=None: (status, body)


def _req(cap=Capability.COMPANY_IDENTITY, **over):
    base = dict(decision_case_id="dc1", capability=cap, subject_refs=("Acme Corp",),
                purpose="supplier onboarding", tenant="t", max_cost=1.0)
    base.update(over)
    return EvidenceRequest(**base)


# ── GLEIF (open) ────────────────────────────────────────────────────────────────────────────────────────
_GLEIF_OK = {"data": [{"attributes": {"lei": "LEI123", "entity": {"legalName": {"name": "Acme Corp"},
                                                                 "status": "ACTIVE", "jurisdiction": "US"}}}]}


def test_gleif_normalizes_to_governed_artifact():
    p = GleifProvider(fetch=_fetch(200, _GLEIF_OK))
    assert p.check_entitlement("t", Capability.COMPANY_IDENTITY)   # open — always entitled
    res = p.acquire(_req())
    assert res.ok
    a = res.artifacts[0]
    assert a.provider == "gleif" and a.observations[0]["lei"] == "LEI123"
    assert a.has_provenance() and a.cost == 0.0 and "GLEIF" in a.license_scope


def test_gleif_no_match_and_unavailable():
    assert GleifProvider(fetch=_fetch(200, {"data": []})).acquire(_req()).failure == AcquisitionFailure.NO_MATCH
    assert GleifProvider(fetch=_fetch(503, {})).acquire(_req()).failure == AcquisitionFailure.UNAVAILABLE
    assert GleifProvider(fetch=_fetch(429, {})).acquire(_req()).failure == AcquisitionFailure.RATE_LIMITED


# ── OpenSanctions (open via yente / keyed hosted) ─────────────────────────────────────────────────────────
def test_opensanctions_entitlement_open_vs_hosted():
    hosted_nokey = OpenSanctionsProvider(fetch=_fetch(200, {"results": []}))
    assert not hosted_nokey.check_entitlement("t", Capability.SANCTIONS_RISK)   # hosted needs a key
    assert OpenSanctionsProvider(api_key="k").check_entitlement("t", Capability.SANCTIONS_RISK)
    assert OpenSanctionsProvider(base_url="http://yente.local").check_entitlement("t", Capability.SANCTIONS_RISK)


def test_opensanctions_clean_screen_is_evidence_not_failure():
    p = OpenSanctionsProvider(base_url="http://yente.local", fetch=_fetch(200, {"results": []}))
    res = p.acquire(_req(cap=Capability.SANCTIONS_RISK, subject_refs=("Jane Roe",)))
    assert res.ok and res.artifacts[0].observations[0] == {"match": False, "hits": 0}


def test_opensanctions_hit_scores_confidence():
    body = {"results": [{"id": "ofac-1", "caption": "BADCO", "schema": "Company", "score": 0.92,
                         "datasets": ["us_ofac"], "properties": {"topics": ["sanction"]}}]}
    p = OpenSanctionsProvider(api_key="k", fetch=_fetch(200, body))
    res = p.acquire(_req(cap=Capability.SANCTIONS_RISK, subject_refs=("BADCO",)))
    assert res.ok and res.artifacts[0].observations[0]["match"] is True
    assert res.artifacts[0].confidence == 0.92


def test_opensanctions_auth_failure():
    p = OpenSanctionsProvider(api_key="bad", fetch=_fetch(403, {}))
    assert p.acquire(_req(cap=Capability.SANCTIONS_RISK)).failure == AcquisitionFailure.NOT_ENTITLED


# ── OpenCorporates (BYO token) ────────────────────────────────────────────────────────────────────────────
def test_opencorporates_requires_token():
    p = OpenCorporatesProvider()  # no token
    assert not p.check_entitlement("t", Capability.COMPANY_IDENTITY)
    assert p.acquire(_req()).failure == AcquisitionFailure.NOT_ENTITLED
    body = {"results": {"companies": [{"company": {"name": "Acme Corp", "company_number": "123",
                                                   "jurisdiction_code": "us_de"}}]}}
    keyed = OpenCorporatesProvider(api_token="tok", fetch=_fetch(200, body))
    assert keyed.check_entitlement("t", Capability.COMPANY_IDENTITY)
    res = keyed.acquire(_req())
    assert res.ok and res.artifacts[0].observations[0]["company_number"] == "123" and res.artifacts[0].cost > 0


# ── Discovery bridge + value accounting ──────────────────────────────────────────────────────────────────
def test_discovery_bridge_routes_records_and_gates():
    reg = IntelligenceRegistry()
    reg.register(GleifProvider(fetch=_fetch(200, _GLEIF_OK)))          # open, free
    reg.register(OpenCorporatesProvider())                            # BYO, unentitled (no token)
    with tempfile.TemporaryDirectory() as d:
        store = EvidenceValueStore(f"{d}/ledger.jsonl")
        res, rec, trace = acquire_for_decision(reg, _req(), store=store)
        assert res.ok and res.artifacts[0].provider == "gleif"       # entitled+cheapest chosen
        assert rec.evidence_received and store.records()[0].provider == "gleif"
        # value gate: evidence that can't change the action is skipped, no spend, still recorded
        res2, rec2, trace2 = acquire_for_decision(reg, _req(decision_case_id="dc2"),
                                                  value_fn=lambda r: 0.0, store=store)
        assert not res2.ok and any("cannot change" in t for t in trace2)
        assert len(store.records()) == 2
        summ = store.summary()
        assert summ[("gleif", "company_identity")]["lookups"] == 1   # the acquired one
        assert summ[("", "company_identity")]["lookups"] == 1        # the gate-skipped one (no provider)


def test_value_store_resolve_outcome_and_summary():
    with tempfile.TemporaryDirectory() as d:
        reg = IntelligenceRegistry(); reg.register(GleifProvider(fetch=_fetch(200, _GLEIF_OK)))
        store = EvidenceValueStore(f"{d}/l.jsonl")
        acquire_for_decision(reg, _req(), store=store)
        assert store.resolve_outcome("dc1", "beneficial") == 1
        assert store.records()[0].verified_outcome == "beneficial"


def test_default_registry_composition():
    reg = default_registry()  # no keys → GLEIF open, OpenSanctions hosted-unentitled, OpenCorporates unentitled
    assert reg.match(_req(cap=Capability.COMPANY_IDENTITY))[0].provider_id == "gleif"
    assert reg.match(_req(cap=Capability.SANCTIONS_RISK)) == ()    # no entitled sanctions provider without a key
    keyed = default_registry(opensanctions_base="http://yente.local")
    assert keyed.match(_req(cap=Capability.SANCTIONS_RISK))[0].provider_id == "opensanctions"


# ── OpenFIGI (open, POST seam, ASSET_IDENTITY) ───────────────────────────────────────────────────────────
from agentic_os.intelligence.adapters.companies_house import CompaniesHouseProvider  # noqa: E402
from agentic_os.intelligence.adapters.open_ownership import OpenOwnershipProvider  # noqa: E402
from agentic_os.intelligence.adapters.openfigi import OpenFigiProvider  # noqa: E402
from agentic_os.intelligence.adapters.sec_edgar import SecEdgarProvider  # noqa: E402


def _fetch_post(status, body):
    return lambda method, url, headers=None, body_json=None: (status, body)


def test_openfigi_open_and_normalizes():
    body = {"data": [{"figi": "BBG000B9XRY4", "name": "APPLE INC", "ticker": "AAPL", "exchCode": "US",
                      "securityType": "Common Stock", "marketSector": "Equity", "compositeFIGI": "BBG000B9XVV8"}]}
    p = OpenFigiProvider(fetch=_fetch_post(200, body))
    assert p.check_entitlement("t", Capability.ASSET_IDENTITY)  # open — always entitled
    res = p.acquire(_req(cap=Capability.ASSET_IDENTITY, subject_refs=("Apple",)))
    assert res.ok
    a = res.artifacts[0]
    assert a.provider == "openfigi" and a.observations[0]["figi"] == "BBG000B9XRY4" and a.cost == 0.0
    assert a.has_provenance() and "OpenFIGI" in a.license_scope


def test_openfigi_no_match_and_rate_limit():
    assert OpenFigiProvider(fetch=_fetch_post(200, {"data": []})).acquire(
        _req(cap=Capability.ASSET_IDENTITY)).failure == AcquisitionFailure.NO_MATCH
    assert OpenFigiProvider(fetch=_fetch_post(429, {})).acquire(
        _req(cap=Capability.ASSET_IDENTITY)).failure == AcquisitionFailure.RATE_LIMITED


# ── SEC EDGAR (open, COMPANY_IDENTITY) ───────────────────────────────────────────────────────────────────
def test_sec_edgar_open_and_collapses_to_filers():
    body = {"hits": {"hits": [
        {"_source": {"ciks": ["0000320193"], "display_names": ["Apple Inc. (AAPL) (CIK 0000320193)"],
                     "file_date": "2026-01-15", "root_forms": "10-K"}},
        {"_source": {"ciks": ["0000320193"], "display_names": ["Apple Inc. (AAPL) (CIK 0000320193)"],
                     "file_date": "2025-10-30", "root_forms": "10-Q"}}]}}
    p = SecEdgarProvider(fetch=_fetch(200, body))
    assert p.check_entitlement("t", Capability.COMPANY_IDENTITY)  # open
    res = p.acquire(_req(subject_refs=("Apple Inc",)))
    assert res.ok
    a = res.artifacts[0]
    assert len(a.observations) == 1 and a.observations[0]["cik"] == "0000320193"  # two filings → one filer
    assert a.cost == 0.0 and "EDGAR" in a.license_scope


def test_sec_edgar_no_match():
    assert SecEdgarProvider(fetch=_fetch(200, {"hits": {"hits": []}})).acquire(
        _req()).failure == AcquisitionFailure.NO_MATCH


# ── UK Companies House (BYO free key; identity + beneficial ownership) ────────────────────────────────────
def test_companies_house_requires_key():
    p = CompaniesHouseProvider()  # no key
    assert not p.check_entitlement("t", Capability.COMPANY_IDENTITY)
    assert p.acquire(_req()).failure == AcquisitionFailure.NOT_ENTITLED


def test_companies_house_identity_and_psc():
    search = {"items": [{"title": "MONZO BANK LIMITED", "company_number": "09446231",
                         "company_status": "active", "company_type": "ltd", "date_of_creation": "2015-02-06"}]}
    ident = CompaniesHouseProvider(api_key="k", fetch=_fetch(200, search))
    assert ident.check_entitlement("t", Capability.COMPANY_IDENTITY)
    r1 = ident.acquire(_req(subject_refs=("Monzo",)))
    assert r1.ok and r1.artifacts[0].observations[0]["company_number"] == "09446231"
    assert r1.artifacts[0].capability == Capability.COMPANY_IDENTITY

    psc = {"items": [{"name": "Mr Tom Blomfield", "kind": "individual-person-with-significant-control",
                      "natures_of_control": ["ownership-of-shares-25-to-50-percent"], "nationality": "British"}]}
    bo = CompaniesHouseProvider(api_key="k", fetch=_fetch(200, psc))
    r2 = bo.acquire(_req(cap=Capability.BENEFICIAL_OWNERSHIP, subject_refs=("09446231",)))
    assert r2.ok and r2.artifacts[0].capability == Capability.BENEFICIAL_OWNERSHIP
    assert r2.artifacts[0].observations[0]["name"] == "Mr Tom Blomfield"


# ── Open Ownership (open, BENEFICIAL_OWNERSHIP) ──────────────────────────────────────────────────────────
def test_open_ownership_open_and_normalizes():
    body = {"results": [{"statementID": "oo-stmt-1", "name": "Globex Holdings", "statementType": "entityStatement",
                         "interests": [{"type": "shareholding", "share": {"exact": 75}}], "jurisdiction": "gb"}]}
    p = OpenOwnershipProvider(fetch=_fetch(200, body))
    assert p.check_entitlement("t", Capability.BENEFICIAL_OWNERSHIP)  # open
    res = p.acquire(_req(cap=Capability.BENEFICIAL_OWNERSHIP, subject_refs=("Globex",)))
    assert res.ok and res.artifacts[0].observations[0]["statement_id"] == "oo-stmt-1"
    assert res.artifacts[0].cost == 0.0 and "Open Ownership" in res.artifacts[0].license_scope


# ── default_registry now carries the free Counterparty/Asset baseline ────────────────────────────────────
def test_default_registry_includes_free_counterparty_providers():
    reg = default_registry()  # no keys
    assert reg.match(_req(cap=Capability.ASSET_IDENTITY))[0].provider_id == "openfigi"        # open, free
    assert reg.match(_req(cap=Capability.BENEFICIAL_OWNERSHIP))[0].provider_id == "open_ownership"  # open, free
    ids = {p.provider_id for p in reg.all()}
    assert {"gleif", "sec_edgar", "openfigi", "open_ownership"} <= ids
    # Companies House is BYO (keyed) → beneficial-ownership still resolves to the open provider without a key
    keyed = default_registry(companies_house_key="k")
    ch = [p.provider_id for p in keyed.match(_req(cap=Capability.BENEFICIAL_OWNERSHIP))]
    assert "companies_house" in ch and "open_ownership" in ch
