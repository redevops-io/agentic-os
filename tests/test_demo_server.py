"""The deployable §3 reference server — endpoints wired + bi-temporal KYC screen. Offline TestClient."""
from __future__ import annotations

from starlette.testclient import TestClient

from agentic_os.intelligence.demo_server import build_app


def _client():
    return TestClient(build_app())


def test_index_lists_capabilities_and_endpoints():
    r = _client().get("/")
    assert r.status_code == 200
    body = r.json()
    assert "shortage_risk" in body["capabilities"] and "payment_behavior" in body["capabilities"]
    assert any("kyc/screen" in e for e in body["endpoints"])


def test_index_serves_html_landing_to_browsers():
    c = _client()
    html = c.get("/", headers={"accept": "text/html"})
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    # the landing carries the interactive KYC widget + a real served capability chip
    assert 'id="applicant"' in html.text and "order_lineage" in html.text
    # API clients still get the machine-readable index
    assert "capabilities" in c.get("/", headers={"accept": "application/json"}).json()


def test_shortage_risk_resolves_over_the_demo_data():
    r = _client().post("/v1/intelligence/supply/shortage_risk", json={"subject_refs": ["Rim", "A"], "tenant": "t"})
    assert r.status_code == 200 and r.json()["capability"] == "shortage_risk"


def test_kyc_screen_is_bitemporal():
    c = _client()
    before = c.get("/v1/intelligence/kyc/screen",
                   params={"applicant": "handlowy", "as_of": "2026-01-15T00:00:00Z"})
    after = c.get("/v1/intelligence/kyc/screen",
                  params={"applicant": "handlowy", "as_of": "2026-03-15T00:00:00Z"})
    assert before.json()["decision"] == "GO"                       # sanction not yet known
    assert after.json()["decision"] == "NO-GO" and after.json()["flagged"] == "CITIGROUP INC."


def test_kyc_screen_ambiguous_is_abstain():
    r = _client().get("/v1/intelligence/kyc/screen", params={"applicant": "abb", "as_of": "2026-06-01T00:00:00Z"})
    assert r.json()["decision"] == "ABSTAIN"


def test_quote_endpoint_available():
    r = _client().post("/v1/intelligence/quote",
                       json={"capability": "payment_behavior", "subject_refs": ["cust:acme"], "tenant": "t"})
    assert r.status_code == 200 and r.json()["available"] is True


def _quote_source(svc):
    """The RevenueStateSource currently bound to QUOTE_FEASIBILITY."""
    from runtime_contracts.protocol import Capability
    binding = svc._bindings[Capability.QUOTE_FEASIBILITY]
    return next(iter(binding.registry.all())).source


def test_quote_feasibility_stays_synthetic_without_erpnext(monkeypatch):
    """No ERPNext env → the open demo (grc:8814) binds the synthetic in-memory quote source, unchanged."""
    from agentic_os.integrations import erpnext as erp_mod
    from agentic_os.intelligence.demo_server import build_service
    from agentic_os.intelligence.families import InMemoryRevenueState
    monkeypatch.setattr(erp_mod, "erpnext_from_env", lambda: None)
    assert isinstance(_quote_source(build_service()), InMemoryRevenueState)


def test_quote_feasibility_binds_live_erpnext_when_configured(monkeypatch):
    """ERPNext configured → QUOTE_FEASIBILITY resolves against the live catalog (ErpnextCatalogState)."""
    from agentic_os.integrations import erpnext as erp_mod
    from agentic_os.integrations.erpnext import ErpnextClient
    from agentic_os.intelligence.demo_server import build_service
    from agentic_os.intelligence.families import ErpnextCatalogState

    class _StubErpnext(ErpnextClient):
        def __init__(self):
            super().__init__(base_url="http://stub", api_key="k", api_secret="s")

    monkeypatch.setattr(erp_mod, "erpnext_from_env", lambda: _StubErpnext())
    src = _quote_source(build_service())
    assert isinstance(src, ErpnextCatalogState)
    assert "cust:acme" in src.requests        # the demo subject is wired to the live catalog
