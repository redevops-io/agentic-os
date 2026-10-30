"""/v1/intelligence with an authenticator: the caller is authenticated and the tenant comes from the principal.

Also the demo server's explicit unauthenticated-demo behaviour (startup log, loopback-only bind unless opted in).
Synthetic tokens and data only.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from agentic_os.agent_gateway import DevTokenVerifier
from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import InventoryPosition, PurchaseOrder
from agentic_os.intelligence import IntelligenceService, build_router
from agentic_os.intelligence.auth import (
    GatewayVerifierAuthenticator, IntelligencePrincipal, StaticBearerAuthenticator)
from agentic_os.intelligence.families import SupplyGraph, supply_registry, supply_synthesize

TOKEN_A = "synthetic-token-for-tenant-a"
TOKEN_B = "synthetic-token-for-tenant-b"
A = {"Authorization": f"Bearer {TOKEN_A}"}
B = {"Authorization": f"Bearer {TOKEN_B}"}
BODY = {"subject_refs": ["Rim", "A"]}
RESOLVE = "/v1/intelligence/supply/shortage_risk"


class _Counting(IntelligenceService):
    """Counts the calls that reach the service, so a refused request can be shown to have reached nothing."""
    calls = 0

    def resolve(self, need, **kw):
        type(self).calls += 1
        return super().resolve(need, **kw)

    def quote(self, need):
        type(self).calls += 1
        return super().quote(need)


def _service():
    def pr(r):
        return Provenance(provider="erp", provider_ref=r)
    graph = SupplyGraph(
        inventory=[InventoryPosition(prov=pr("i"), part="Rim", site="A", on_hand=20.0)],
        open_supply=[PurchaseOrder(prov=pr("po1"), supplier_ref="s", site="A", part="Rim", quantity=50.0,
                                   promised_date="2026-02-15", ordered_at="2026-01-01T00:00:00Z")],
        demand=[])
    _Counting.calls = 0
    return _Counting().bind(supply_registry(graph), supply_synthesize)


def _client(authenticator=None):
    app = FastAPI()
    app.include_router(build_router(_service(), authenticator=authenticator))
    return TestClient(app)


def _authed():
    return _client(StaticBearerAuthenticator({TOKEN_A: ("svc-a", "tenant-a"), TOKEN_B: ("svc-b", "tenant-b")}))


# ── anonymous and wrong credentials are refused on every route ──────────────────────────────────────────
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN_A},
                                     {"Authorization": "Basic " + TOKEN_A}, {"Authorization": "Bearer "},
                                     {"Authorization": "Bearer " + TOKEN_A + "x"}])
def test_anonymous_and_wrong_credentials_are_401_on_every_route(headers):
    c = _authed()
    assert c.get("/v1/intelligence/capabilities", headers=headers).status_code == 401
    assert c.post("/v1/intelligence/quote", headers=headers, json={"capability": "shortage_risk", **BODY}).status_code == 401
    assert c.post(RESOLVE, headers=headers, json=BODY).status_code == 401
    assert c.get("/v1/intelligence/requests/anything", headers=headers).status_code == 401
    assert _Counting.calls == 0                       # nothing reached the service
    r = c.get("/v1/intelligence/capabilities", headers=headers)
    assert r.headers.get("www-authenticate") == "Bearer"


def test_a_non_ascii_credential_is_401_not_a_server_error():
    r = _authed().get("/v1/intelligence/capabilities", headers={"Authorization": "Bearer tést".encode("latin-1")})
    assert r.status_code == 401


# ── the tenant comes from the principal ─────────────────────────────────────────────────────────────────
def test_the_tenant_is_the_principals_when_the_body_names_none():
    r = _authed().post(RESOLVE, headers=A, json=BODY)
    assert r.status_code == 200 and r.json()["tenant"] == "tenant-a"
    q = _authed().post("/v1/intelligence/quote", headers=B, json={"capability": "shortage_risk", **BODY})
    assert q.status_code == 200


def test_a_matching_body_tenant_is_accepted():
    r = _authed().post(RESOLVE, headers=A, json={**BODY, "tenant": "tenant-a"})
    assert r.status_code == 200 and r.json()["tenant"] == "tenant-a"


@pytest.mark.parametrize("claimed", ["tenant-b", "tenant-a ", "TENANT-A", "*", "demo"])
def test_a_caller_supplied_other_tenant_is_refused_and_never_reaches_the_service(claimed):
    c = _authed()
    r = c.post(RESOLVE, headers=A, json={**BODY, "tenant": claimed})
    assert r.status_code == 403
    q = c.post("/v1/intelligence/quote", headers=A, json={"capability": "shortage_risk", "tenant": claimed, **BODY})
    assert q.status_code == 403
    assert _Counting.calls == 0


def test_a_stored_result_of_another_tenant_is_not_returned():
    c = _authed()
    made = c.post(RESOLVE, headers=A, json=BODY).json()
    rid = made["request_id"]
    assert c.get(f"/v1/intelligence/requests/{rid}", headers=A).status_code == 200      # its owner
    other = c.get(f"/v1/intelligence/requests/{rid}", headers=B)                          # another tenant
    assert other.status_code == 404 and other.json() == c.get("/v1/intelligence/requests/none", headers=B).json()


def test_the_same_request_by_two_tenants_yields_two_tenant_scoped_results():
    c = _authed()
    ra = c.post(RESOLVE, headers=A, json=BODY).json()
    rb = c.post(RESOLVE, headers=B, json=BODY).json()
    assert ra["tenant"] == "tenant-a" and rb["tenant"] == "tenant-b" and ra["request_id"] != rb["request_id"]


# ── authenticators ──────────────────────────────────────────────────────────────────────────────────────
def test_a_principal_without_a_usable_tenant_is_403():
    class _NoTenant:
        def authenticate(self, authorization):
            return IntelligencePrincipal("svc", " ") if authorization else None
    c = _client(_NoTenant())
    assert c.get("/v1/intelligence/capabilities", headers=A).status_code == 403
    assert c.get("/v1/intelligence/capabilities").status_code == 401


def test_static_authenticator_rejects_unusable_configuration():
    for bad in ({}, {"": ("s", "t")}, {"tok": ("", "t")}, {"tok": ("s", "")}, {"tok": ("s", " t")}):
        with pytest.raises(ValueError):
            StaticBearerAuthenticator(bad)


def test_static_authenticator_compares_with_hmac_compare_digest(monkeypatch):
    import hmac
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    auth = StaticBearerAuthenticator({TOKEN_A: ("svc-a", "tenant-a"), TOKEN_B: ("svc-b", "tenant-b")})
    assert auth.authenticate(f"Bearer {TOKEN_B}") == IntelligencePrincipal("svc-b", "tenant-b")
    assert len(calls) == 2                            # every configured token was compared, no early exit


def test_the_gateway_token_verifier_can_authenticate_the_router():
    verifier = DevTokenVerifier()
    token = verifier.issue("agent-1", tenant="tenant-z")
    c = _client(GatewayVerifierAuthenticator(verifier))
    r = c.post(RESOLVE, headers={"Authorization": f"Bearer {token}"}, json=BODY)
    assert r.status_code == 200 and r.json()["tenant"] == "tenant-z"
    assert c.post(RESOLVE, headers={"Authorization": "Bearer gwt_unknown"}, json=BODY).status_code == 401
    assert c.post(RESOLVE, headers={"Authorization": f"Bearer {token}"}, json={**BODY, "tenant": "tenant-a"}).status_code == 403


# ── the default (no authenticator) is the unchanged unauthenticated demo, and says so ───────────────────
def test_without_an_authenticator_the_demo_behaviour_is_unchanged_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="agentic_os.intelligence"):
        c = _client()
    assert any("WITHOUT an authenticator" in r.message for r in caplog.records)
    r = c.post(RESOLVE, json={**BODY, "tenant": "whatever"})
    assert r.status_code == 200 and r.json()["tenant"] == "whatever"


# ── demo server ─────────────────────────────────────────────────────────────────────────────────────────
def test_demo_server_says_it_is_an_unauthenticated_demo_at_startup(caplog):
    from agentic_os.intelligence.demo_server import build_app
    with caplog.at_level(logging.WARNING, logger="agentic_os.intelligence.demo"):
        with TestClient(build_app()) as c:
            assert c.get("/v1/intelligence/capabilities").status_code == 200
    assert any("UNAUTHENTICATED DEMO" in r.message for r in caplog.records)


def test_demo_server_documents_itself_as_unauthenticated():
    from agentic_os.intelligence import demo_server, api
    assert "UNAUTHENTICATED DEMO" in demo_server.__doc__ and "UNAUTHENTICATED DEMO" in api.__doc__


@pytest.mark.parametrize("host,loopback", [("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
                                           ("127.5.5.5", True), ("0.0.0.0", False), ("::", False), ("", False),
                                           ("192.0.2.10", False), ("example.com", False), ("localhost.example.com", False)])
def test_is_loopback_host(host, loopback):
    from agentic_os.intelligence.demo_server import is_loopback_host
    assert is_loopback_host(host) is loopback


@pytest.fixture
def served(monkeypatch):
    import uvicorn
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: ran.append((host, port)))
    monkeypatch.delenv("INTELLIGENCE_DEMO_ALLOW_NON_LOOPBACK", raising=False)
    return ran


def test_demo_main_serves_loopback_by_default(served):
    from agentic_os.intelligence.demo_server import main
    assert main([]) == 0 and served == [("127.0.0.1", 8814)]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "example.com"])
def test_demo_main_refuses_a_non_loopback_bind_without_opt_in(served, capsys, host):
    from agentic_os.intelligence.demo_server import main
    assert main(["--host", host]) == 2 and served == []
    assert "refusing to bind" in capsys.readouterr().err


def test_demo_main_serves_non_loopback_only_with_the_explicit_opt_in(served, monkeypatch):
    from agentic_os.intelligence.demo_server import main
    assert main(["--host", "0.0.0.0", "--allow-non-loopback"]) == 0
    monkeypatch.setenv("INTELLIGENCE_DEMO_ALLOW_NON_LOOPBACK", "1")
    assert main(["--host", "192.0.2.10"]) == 0
    assert served == [("0.0.0.0", 8814), ("192.0.2.10", 8814)]
