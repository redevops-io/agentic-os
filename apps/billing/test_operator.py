"""agentic-billing as a Mission Runtime operator — proof over a fake Lago core.

Exercises the REAL core logic (fetch_activity KPIs + chase_overdue retry) and the SDK-mount
contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's own
HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/billing/test_operator.py -q
"""
from __future__ import annotations

import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from billing import core
from billing.operator import build_billing_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake Lago core (one overdue invoice) ───────────────────────────────────
INVOICES = [{
    "number": "INV-001", "customer": {"name": "Acme Roofing"}, "total_amount_cents": 45000,
    "status": "finalized", "payment_status": "failed", "payment_overdue": True,
    "payment_due_date": "2026-06-01", "lago_id": "lago-1", "issuing_date": "2026-05-15",
}]
CUSTOMERS = [{"name": "Acme Roofing"}]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(url, timeout=None, headers=None, params=None):
    return _Resp(200)  # /health


class _FakeClient:
    """Stands in for httpx.Client — records retry_payment/subscription POSTs + DELETEs."""
    posted: list[str] = []
    deleted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/api/v1/invoices"):
            return _Resp(200, {"invoices": INVOICES, "meta": {"total_pages": 1}})
        if url.endswith("/api/v1/customers"):
            return _Resp(200, {"customers": CUSTOMERS, "meta": {"total_pages": 1}})
        return _Resp(200, {})

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append(url)
        if url.endswith("/api/v1/subscriptions"):
            sub = (json or {}).get("subscription", {})
            return _Resp(200, {"subscription": {"lago_id": "lago-sub-1",
                                                "external_id": sub.get("external_id")}})
        return _Resp(200)

    def delete(self, url, headers=None):
        _FakeClient.deleted.append(url)
        return _Resp(200)


@pytest.fixture(autouse=True)
def _fake_lago(monkeypatch):
    _FakeClient.posted = []
    _FakeClient.deleted = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "LAGO_API_KEY", "test-key")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: _FakeClient()))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_billing_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {
        "billing.summary", "billing.dunning", "billing.refund",
        "billing.create_subscription", "billing.cancel_subscription",
    }
    # money-moving capabilities are gated (matches modules.yaml refund + dunning gates)
    assert caps["billing.dunning"]["approval_required"] is True
    assert caps["billing.refund"]["approval_required"] is True
    assert caps["billing.summary"]["approval_required"] is False
    assert "billing_activity" in caps["billing.summary"]["provides"]
    # onboarding: create_subscription provides `subscription`, is NOT gated, and its
    # saga compensation is billing.cancel_subscription.
    cs = caps["billing.create_subscription"]
    assert "subscription" in cs["provides"]
    assert cs["undo"] == "billing.cancel_subscription"
    assert cs["approval_required"] is False
    assert cs["side_effecting"] is True
    assert "billing:write" in cs["permissions"]
    assert caps["billing.cancel_subscription"]["side_effecting"] is True


def test_invoke_create_subscription_posts_to_lago(client):
    r = client.post("/invoke", json={
        "capability": "billing.create_subscription",
        "inputs": {"customer": "Acme Roofing", "plan_code": "starter_monthly"},
    }).json()
    res = r["result"]
    assert res["status"] == "done" and res["action"] == "create_subscription"
    assert res["subscription_id"] and res["plan"] == "starter_monthly"
    # the REAL core POSTed a subscription to Lago's subscriptions endpoint
    assert _FakeClient.posted == ["http://localhost:3000/api/v1/subscriptions"]


def test_invoke_cancel_subscription_deletes_it(client):
    r = client.post("/invoke", json={
        "capability": "billing.cancel_subscription",
        "inputs": {"customer": "Acme Roofing", "plan_code": "starter_monthly"},
    }).json()
    res = r["result"]
    assert res["status"] == "done" and res["cancelled"] is True
    # DELETE against the derived external_id (terminate)
    assert len(_FakeClient.deleted) == 1
    assert _FakeClient.deleted[0].startswith("http://localhost:3000/api/v1/subscriptions/")
    assert _FakeClient.posted == []  # cancel does not POST


def test_invoke_dunning_hits_real_lago(client):
    r = client.post("/invoke", json={"capability": "billing.dunning", "inputs": {}}).json()
    res = r["result"]
    assert res["status"] == "done" and res["overdue_count"] == 1
    # the REAL core issued a retry_payment against the (fake) Lago for the overdue invoice
    assert _FakeClient.posted == ["http://localhost:3000/api/v1/invoices/lago-1/retry_payment"]


def test_summary_is_readonly_and_computes_kpis(client):
    res = client.post("/invoke", json={"capability": "billing.summary", "inputs": {}}).json()["result"]
    assert res["core"] == "lago" and res["counts"]["overdue"] == 1
    assert _FakeClient.posted == []  # read-only: no writes to Lago


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "billing.dunning", "inputs": {}, "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    assert _FakeClient.posted == ["http://localhost:3000/api/v1/invoices/lago-1/retry_payment"]  # once


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"billing": "http://billing"}, transport=_transport)
    res = oc.invoke("billing", "billing.summary", {}, idempotency_key="m-1")
    assert res["core"] == "lago" and res["counts"]["overdue"] == 1

    dun = oc.invoke("billing", "billing.dunning", {}, idempotency_key="m-2")
    assert dun["status"] == "done" and dun["overdue_count"] == 1
