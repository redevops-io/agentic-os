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
