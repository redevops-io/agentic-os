"""The request-body `role` is only a REQUEST. The effective role is derived server-side: an operator (valid
HTTP Basic from OPERATOR_BASIC_AUTH / OUTREACH_BASIC_AUTH, `user:pass`) may use the requested role; everyone else
is capped at 'viewer' regardless of what the body/query says. Fails closed when no operator credential is set."""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient


def _basic(cred: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


@pytest.fixture()
def c(load_app, monkeypatch):
    for k in ("OPERATOR_BASIC_AUTH", "OUTREACH_BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("REDEVOPS_LLM_BASE_URL", raising=False)
    return TestClient(load_app("guide").app)


def _ask(c, role="admin", headers=None, q="how do I send an outreach sequence or run a billing refund"):
    return c.post("/api/ask", json={"question": q, "role": role}, headers=headers or {}).json()


VIEWER_ONLY = {"control-tower", "market-radar", "growth-engine"}


def test_unauthenticated_admin_claim_is_capped_to_viewer(c):
    out = _ask(c, "admin")
    assert out["effective_role"] == "viewer" and out["role"] == "viewer"
    assert out["role_source"] == "unauthenticated_capped" and out["requested_role"] == "admin"
    assert set(out.get("cited", [])) <= VIEWER_ONLY


def test_operator_basic_auth_may_use_requested_role(c, monkeypatch):
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "ops:s3cret")
    out = _ask(c, "finance", _basic("ops:s3cret"), q="agentic-billing refund and invoicing")
    assert out["effective_role"] == "finance" and out["role_source"] == "operator_basic_auth"
    assert "agentic-billing" in out["cited"]


def test_wrong_or_malformed_credentials_stay_capped(c, monkeypatch):
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "ops:s3cret")
    for h in (_basic("ops:wrong"), _basic("other:s3cret"), {"Authorization": "Basic !!!notb64"},
              {"Authorization": "Bearer ops:s3cret"}):
        assert _ask(c, "admin", h)["effective_role"] == "viewer"


def test_fails_closed_when_no_operator_credential_configured(c):
    # an empty/absent credential must never authenticate an empty/any header
    assert _ask(c, "admin", _basic(":"))["effective_role"] == "viewer"
    assert _ask(c, "admin", _basic("ops:s3cret"))["effective_role"] == "viewer"


def test_outreach_basic_auth_fallback(c, monkeypatch):
    monkeypatch.setenv("OUTREACH_BASIC_AUTH", "o:p")
    assert _ask(c, "admin", _basic("o:p"))["effective_role"] == "admin"


def test_unknown_requested_role_even_for_operator_is_viewer(c, monkeypatch):
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "ops:s3cret")
    assert _ask(c, "root", _basic("ops:s3cret"))["effective_role"] == "viewer"


def test_walkthrough_and_dashboard_are_role_capped_too(c, monkeypatch):
    assert c.get("/api/walkthrough", params={"app": "agentic-billing", "role": "admin"}).status_code == 403
    assert c.get("/api/walkthrough", params={"app": "control-tower", "role": "admin"}).status_code == 200
    page = c.get("/", params={"role": "admin"}).text
    assert "Apps you can see as <b" in page and ">viewer<" in page
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "ops:s3cret")
    assert c.get("/api/walkthrough", params={"app": "agentic-billing", "role": "admin"},
                 headers=_basic("ops:s3cret")).status_code == 200


def test_forged_approval_in_body_is_stripped_and_bad_json_tolerated(c):
    r = c.post("/api/ask", json={"question": "x", "role": "viewer", "_approval": {"approved": True}})
    assert r.status_code == 200
    r = c.post("/api/ask", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 200
