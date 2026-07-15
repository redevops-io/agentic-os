"""lifecycle as a Mission Runtime operator — proof over a fake Listmonk core.

Exercises the REAL core logic (compose_campaign draft POST + segment/suggest_flow skeletons)
and the SDK-mount contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission
Runtime's own HTTPOperatorClient driving the operator over the wire with exactly-once
idempotency.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/lifecycle/test_operator.py -q
"""
from __future__ import annotations

import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lifecycle import core
from lifecycle.operator import build_lifecycle_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake Listmonk core (one list, one draft campaign) ──────────────────────
LISTS = [{"name": "Newsletter", "type": "public", "subscriber_count": 1200}]
CAMPAIGNS = [{"name": "Spring", "subject": "Hi there", "status": "draft", "lists": [],
              "sent": 0, "views": 5, "created_at": "2026-05-01"}]


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload or {}
        self.text = text

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(url, timeout=None, headers=None, params=None):
    return _Resp(200)  # /health + connectivity probe on /api/lists


class _FakeClient:
    """Stands in for httpx.Client — records campaign-draft POSTs."""
    posted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/api/lists"):
            return _Resp(200, {"data": {"results": LISTS}})
        if url.endswith("/api/campaigns"):
            return _Resp(200, {"data": {"results": CAMPAIGNS}})
        if url.endswith("/api/subscribers"):
            return _Resp(200, {"data": {"total": 1200}})
        return _Resp(200, {"data": {}})

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append(url)
        return _Resp(201, {"data": {"id": 42}})


@pytest.fixture(autouse=True)
def _fake_listmonk(monkeypatch):
    _FakeClient.posted = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "LISTMONK_API_TOKEN", "test-key")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: _FakeClient()))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_lifecycle_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"lifecycle.compose_campaign", "lifecycle.segment", "lifecycle.suggest_flow"}
    # gate matches modules.yaml `approval_required: [send]`: none of these SEND to contacts.
    # compose writes a DRAFT (side-effecting) but crosses no send gate; the rest are advisory.
    assert caps["lifecycle.compose_campaign"]["side_effecting"] is True
    assert caps["lifecycle.compose_campaign"]["approval_required"] is False
    assert caps["lifecycle.segment"]["side_effecting"] is False
    assert caps["lifecycle.segment"]["approval_required"] is False
    assert caps["lifecycle.suggest_flow"]["approval_required"] is False
    assert "campaign_drafted" in caps["lifecycle.compose_campaign"]["provides"]


def test_invoke_compose_writes_draft_to_real_listmonk(client):
    r = client.post("/invoke", json={"capability": "lifecycle.compose_campaign",
                                      "inputs": {"prompt": "spring roof-check reminder", "list_id": 3}}).json()
    res = r["result"]
    assert res["status"] == "done" and res["campaign_id"] == 42
    assert res["listmonk_status"] == "draft"  # never auto-sent — a human reviews + sends
    # the REAL core issued the draft-create POST against the (fake) Listmonk core
    assert _FakeClient.posted == [core.LISTMONK_API_URL + "/api/campaigns"]


def test_segment_and_flow_are_readonly_advisory(client):
    seg = client.post("/invoke", json={"capability": "lifecycle.segment",
                                        "inputs": {"goal": "win back lapsed customers"}}).json()["result"]
    assert seg["status"] == "done" and seg["action"] == "segment"

    flow = client.post("/invoke", json={"capability": "lifecycle.suggest_flow",
                                         "inputs": {"trigger": "welcome"}}).json()["result"]
    assert flow["status"] == "done" and flow["trigger"] == "welcome"
    assert _FakeClient.posted == []  # advisory: no writes to Listmonk


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "lifecycle.compose_campaign",
            "inputs": {"prompt": "spring roof-check reminder"}, "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    assert _FakeClient.posted == [core.LISTMONK_API_URL + "/api/campaigns"]  # POSTed once


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"lifecycle": "http://lifecycle"}, transport=_transport)
    seg = oc.invoke("lifecycle", "lifecycle.segment", {"goal": "high openers"}, idempotency_key="m-1")
    assert seg["status"] == "done" and seg["action"] == "segment"

    comp = oc.invoke("lifecycle", "lifecycle.compose_campaign",
                     {"prompt": "quarterly roof inspection"}, idempotency_key="m-2")
    assert comp["status"] == "done" and comp["campaign_id"] == 42
