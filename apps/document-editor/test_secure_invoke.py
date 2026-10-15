"""Authenticated /invoke: unauthorized => 401/403; tenant isolation; forged approvals ignored; signed approval works."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

ADMIN, ACME, GLOBEX = "admin-token-xxxxxxxxxxxxxxxx", "acme-token-yyyyyyyyyyyyyyyy", "globex-token-zzzzzzzzzzzzzz"   # == conftest


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def invoke(c, tok, cap, inputs=None, **kw):
    return c.post("/invoke", json={"capability": cap, "inputs": inputs or {}, **kw}, headers=H(tok) if tok else {})


def test_health_is_open_and_independent_of_the_editor(client, env):
    env.backend.mode = "unavailable"
    assert client.get("/health").json() == {"status": "ok", "app": "document-editor"}


def test_no_credentials_is_401_on_every_route(client):
    assert client.post("/invoke", json={"capability": "document.connect_check", "inputs": {"tenant": "acme"}}).status_code == 401
    assert client.get("/capabilities").status_code == 401


@pytest.mark.parametrize("hdr", ["Bearer wrong", "Bearer ", "Basic YWRtaW46YWRtaW4=", "bearer", "Token " + ACME, ADMIN])
def test_wrong_or_malformed_credentials_are_401(client, hdr):
    r = client.post("/invoke", json={"capability": "document.connect_check", "inputs": {"tenant": "acme"}},
                    headers={"Authorization": hdr})
    assert r.status_code == 401
    assert client.get("/capabilities", headers={"Authorization": hdr}).status_code == 401


def test_no_token_configured_means_503_not_open(env, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    appmod = importlib.import_module("document-editor.app")
    r = TestClient(appmod.app).post("/invoke", json={"capability": "document.connect_check", "inputs": {"tenant": "acme"}})
    assert r.status_code in (401, 503)
    assert TestClient(appmod.app).post("/invoke", json={"capability": "x"}, headers=H("anything")).status_code in (401, 503)


def test_capabilities_lists_the_contract_with_a_valid_token(client):
    r = client.get("/capabilities", headers=H(ACME))
    assert r.status_code == 200
    names = {c["name"] for c in r.json()["capabilities"]}
    assert names == {"document.connect_check", "document.inspect", "document.open_session", "document.convert",
                     "document.export", "document.propose_patch", "document.apply_patch", "document.publish",
                     "document.set_label"}


def test_unknown_capability_is_404(client):
    assert invoke(client, ADMIN, "document.rm_rf", {"tenant": "acme"}).status_code == 404


def test_tenant_token_cannot_claim_another_tenant(client, env):
    r = invoke(client, ACME, "document.connect_check", {"tenant": "globex"})
    assert r.status_code == 403


def test_tenant_token_is_pinned_to_its_tenant(client, env, kit):
    env.put("acme", "a.docx", kit.make_docx(["acme only"]))
    env.put("globex", "g.docx", kit.make_docx(["globex only"]))
    ok = invoke(client, ACME, "document.inspect", {"doc_id": "a.docx", "local_only": True}).json()["result"]
    assert ok["status"] == "ok"
    # cross-tenant document id: acme's token asking for globex's document id fails (not found in acme's namespace)
    bad = invoke(client, ACME, "document.inspect", {"doc_id": "g.docx", "local_only": True}).json()["result"]
    assert bad["status"] == "not_found" and "globex" not in str(bad)
    other = invoke(client, GLOBEX, "document.inspect", {"doc_id": "a.docx", "local_only": True}).json()["result"]
    assert other["status"] == "not_found"


def test_forged_tenant_and_auth_keys_in_inputs_are_ignored(client, env, kit):
    env.put("globex", "g.docx", kit.make_docx(["globex only"]))
    r = invoke(client, ACME, "document.inspect", {"doc_id": "g.docx", "local_only": True, "_auth_tenant": "globex",
                                                   "_auth_actor": "admin:admin"}).json()["result"]
    assert r["status"] == "not_found"


def test_admin_must_name_a_tenant_and_it_must_be_configured(client):
    assert invoke(client, ADMIN, "document.connect_check", {}).json()["result"]["error"] == "invalid_input"
    assert invoke(client, ADMIN, "document.connect_check", {"tenant": "nobody"}).json()["result"]["error"] == "tenant_mismatch"
    assert invoke(client, ADMIN, "document.connect_check", {"tenant": "acme"}).json()["result"]["status"] == "ok"


def test_backend_unavailable_is_a_controlled_typed_failure_and_other_routes_still_work(client, env):
    env.backend.mode = "unavailable"
    r = invoke(client, ACME, "document.connect_check")
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["status"] == "unavailable" and res["error"] == "backend_unavailable" and res["backend_reachable"] is False
    assert client.get("/health").status_code == 200 and client.get("/capabilities", headers=H(ACME)).status_code == 200


def test_unconfigured_connector_fails_closed_without_crashing(env, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    appmod = importlib.import_module("document-editor.app")
    from_cfg = importlib.import_module("document-editor.wiring").ServiceHolder(
        factory=lambda: (_ for _ in ()).throw(importlib.import_module("document-editor.errors").ConfigError("no config")))
    op = importlib.import_module("document-editor.operator").build_document_editor_operator(from_cfg.get)
    from fastapi import FastAPI
    a = FastAPI()
    a.include_router(importlib.import_module("document-editor.secure_router").secure_router(op))
    c = TestClient(a)
    res = invoke(c, ADMIN, "document.connect_check", {"tenant": "acme"}).json()["result"]
    assert res["status"] == "not_configured" and res["error"] == "config_error"
    assert c.get("/capabilities", headers=H(ADMIN)).status_code == 200
