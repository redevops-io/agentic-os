"""agentic-privacy as a Mission Runtime operator — proof over fake personal-data cores.

Exercises the REAL core fan-out (access gather + delete erasure across ERPNext / Listmonk /
Chatwoot) and the SDK-mount contract (GET /capabilities + POST /invoke) end-to-end, plus the
Mission Runtime's own HTTPOperatorClient driving the operator over the wire with exactly-once
idempotency on the (irreversible) erasure.

The package dir has a HYPHEN, so it can't be imported with `import agentic-privacy`; we load it
via importlib and fake the fanned-out cores at the httpx boundary (`core.httpx`), the same seam
the billing exemplar monkeypatches.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/agentic-privacy/test_operator.py -q
"""
from __future__ import annotations

import importlib
import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# hyphenated package dir → importlib, not `import agentic-privacy`
core = importlib.import_module("agentic-privacy.core")
operator = importlib.import_module("agentic-privacy.operator")
from agentic_os.mission.operators import HTTPOperatorClient

SUBJECT = "jane@example.com"


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Stands in for httpx.Client — one hit per subject in ERPNext (Lead) + Listmonk + Chatwoot,
    and records every DELETE so we can prove the erasure fan-out ran (exactly once under a key)."""
    deleted: list[str] = []
    put: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if "/api/resource/Lead" in url:
            return _Resp(200, {"data": [{"name": "LEAD-1", "lead_name": "Jane",
                                         "company_name": "Acme", "status": "Open",
                                         "email_id": SUBJECT}]})
        if "/api/resource/Contact" in url or "/api/resource/Communication" in url:
            return _Resp(200, {"data": []})
        if "/api/subscribers?query=" in url:
            return _Resp(200, {"data": {"results": [
                {"id": 7, "email": SUBJECT, "name": "Jane", "lists": [], "status": "enabled"}]}})
        if "/contacts/search" in url:
            return _Resp(200, {"payload": [
                {"id": 42, "name": "Jane", "email": SUBJECT, "phone_number": None}]})
        return _Resp(200, {})

    def delete(self, url, headers=None):
        _FakeClient.deleted.append(url)
        return _Resp(204 if "/contacts/" in url else 200)

    def put(self, url, headers=None, json=None):  # noqa: A003 - mirror httpx api
        _FakeClient.put.append(url)
        return _Resp(200)

    def post(self, url, headers=None, json=None):
        return _Resp(200, {})

    def request(self, method, url, headers=None):
        if method == "DELETE":
            _FakeClient.deleted.append(url)
        return _Resp(200)


@pytest.fixture(autouse=True)
def _fake_cores(monkeypatch, tmp_path):
    _FakeClient.deleted = []
    _FakeClient.put = []
    # redirect the file-backed store to a temp dir (persist/audit)
    monkeypatch.setattr(core, "DATA_DIR", str(tmp_path))
    # no real email sender in tests → intake verification degrades to log-only
    monkeypatch.setattr(core, "POSTMARK_TOKEN", "")
    monkeypatch.setattr(core, "SMTP_HOST", "")
    # configure three real connectors; the rest stay unconfigured (honest coverage)
    monkeypatch.setattr(core, "ERPNEXT_URL", "http://erp")
    monkeypatch.setattr(core, "ERPNEXT_KEY", "k")
    monkeypatch.setattr(core, "ERPNEXT_SEC", "s")
    monkeypatch.setattr(core, "LISTMONK_URL", "http://lm")
    monkeypatch.setattr(core, "LISTMONK_USER", "u")
    monkeypatch.setattr(core, "LISTMONK_TOKEN", "t")
    monkeypatch.setattr(core, "CHATWOOT_URL", "http://cw")
    monkeypatch.setattr(core, "CHATWOOT_TOKEN", "ct")
    monkeypatch.setattr(core, "CHATWOOT_ACCT", "1")
    # fake the httpx boundary the fan-out calls through
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(
        Client=lambda timeout=None: _FakeClient(),
        get=lambda *a, **k: _Resp(200),
        post=lambda *a, **k: _Resp(200, {}),
    ))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(operator.build_privacy_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    assert m["operator"] == "agentic-privacy"
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"privacy.intake", "privacy.access", "privacy.delete", "privacy.retention"}
    # the gate per modules.yaml is [delete] — erasure is the only approval-gated capability
    d = caps["privacy.delete"]
    assert d["approval_required"] is True
    assert d["side_effecting"] is True
    assert "erasure" in d["provides"]
    assert "privacy:erase" in d["permissions"]
    # erasure is irreversible → no saga undo
    assert d["undo"] in (None, "")
    # intake writes (opens a DSAR) but is NOT the gated capability
    assert caps["privacy.intake"]["side_effecting"] is True
    assert caps["privacy.intake"]["approval_required"] is False
    # access + retention are read-only reports, never gated
    assert caps["privacy.access"]["side_effecting"] is False
    assert caps["privacy.access"]["approval_required"] is False
    assert caps["privacy.retention"]["approval_required"] is False
    assert "subject_data" in caps["privacy.access"]["provides"]
    assert "retention_report" in caps["privacy.retention"]["provides"]


def test_access_gathers_across_live_systems_readonly(client):
    res = client.post("/invoke", json={
        "capability": "privacy.access", "inputs": {"email": SUBJECT}}).json()["result"]
    assert res["status"] == "done" and res["type"] == "access"
    # ERPNext(1 Lead) + Listmonk(1) + Chatwoot(1) = 3; unconfigured cores contribute nothing
    assert res["record_count"] == 3
    systems = {s["system"]: s for s in res["systems"]}
    assert systems["erpnext"]["ok"] and systems["billing"]["ok"] is False
    assert _FakeClient.deleted == []  # read-only: access never deletes


def test_delete_fans_out_the_erasure(client):
    res = client.post("/invoke", json={
        "capability": "privacy.delete", "inputs": {"email": SUBJECT}}).json()["result"]
    assert res["status"] == "done" and res["type"] == "delete"
    # erased across all three live connectors (Lead + subscriber + contact)
    assert res["deleted_count"] == 3
    assert len(_FakeClient.deleted) == 3
    assert any("/api/resource/Lead/LEAD-1" in u for u in _FakeClient.deleted)
    assert any("/api/subscribers/7" in u for u in _FakeClient.deleted)
    assert any("/contacts/42" in u for u in _FakeClient.deleted)


def test_intake_opens_a_dsar(client):
    res = client.post("/invoke", json={
        "capability": "privacy.intake",
        "inputs": {"email": SUBJECT, "type": "delete"}}).json()["result"]
    assert res["status"] == "received" and res["type"] == "delete"
    assert res["verification"] == "logged"  # no email sender configured in tests
    assert res["request_id"].startswith("DSAR-")
    assert _FakeClient.deleted == []  # opening a request erases nothing


def test_retention_is_a_dryrun_report(client):
    res = client.post("/invoke", json={
        "capability": "privacy.retention", "inputs": {}}).json()["result"]
    assert res["dry_run"] is True
    assert res["systems"] and res["systems"][0]["system"] == "erpnext"
    assert res["systems"][0]["purged"] == 0  # policy check never purges
    assert _FakeClient.deleted == []


def test_idempotency_dedupes_the_erasure(client):
    body = {"capability": "privacy.delete", "inputs": {"email": SUBJECT}, "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    # replaying the same key must NOT erase twice — three DELETEs total, not six
    assert len(_FakeClient.deleted) == 3


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"agentic-privacy": "http://privacy"}, transport=_transport)
    acc = oc.invoke("agentic-privacy", "privacy.access", {"email": SUBJECT}, idempotency_key="m-1")
    assert acc["status"] == "done" and acc["record_count"] == 3

    dele = oc.invoke("agentic-privacy", "privacy.delete", {"email": SUBJECT}, idempotency_key="m-2")
    assert dele["status"] == "done" and dele["deleted_count"] == 3
