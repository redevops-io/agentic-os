"""agentic-market-radar as a Mission Runtime operator — proof over a fake changedetection core.

Exercises the REAL core logic (fetch_activity KPIs + add_watch POST + brief read) and the
SDK-mount contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's
own HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

The dir name has a hyphen, so the package is imported via importlib.import_module.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/market-radar/test_operator.py -q
"""
from __future__ import annotations

import importlib
import time
import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

core = importlib.import_module("market-radar.core")
build_market_radar_operator = importlib.import_module("market-radar.operator").build_market_radar_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake changedetection core (one changed, unread pricing watch) ──────────
_RECENT = int(time.time()) - 3600  # checked/changed an hour ago → counts this week
SYSTEMINFO = {"version": "0.45.20", "queue_size": 0}
WATCHES = {
    "w-1": {
        "url": "https://competitor.example/pricing",
        "title": "Competitor Pricing",
        "tags": ["tag-price"],
        "last_checked": _RECENT,
        "last_changed": _RECENT,
        "viewed": False,
        "last_error": None,
    }
}
TAGS = {"tag-price": {"title": "pricing"}}
HISTORY = {"1700000000": "snapshot-a", "1700000600": "snapshot-b"}


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(url, timeout=None, headers=None, params=None):
    if url.endswith("/api/v1/systeminfo"):
        return _Resp(200, SYSTEMINFO)
    return _Resp(200, {})


class _FakeClient:
    """Stands in for httpx.Client — records add_watch POSTs; serves watch/tags/history GETs."""
    posted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/api/v1/watch"):
            return _Resp(200, WATCHES)
        if url.endswith("/api/v1/tags"):
            return _Resp(200, TAGS)
        if url.endswith("/history"):
            return _Resp(200, HISTORY)
        return _Resp(200, {})

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append(url)
        return _Resp(201, {"uuid": "new-watch-1"})


@pytest.fixture(autouse=True)
def _fake_cd(monkeypatch):
    _FakeClient.posted = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "CD_API_KEY", "test-key")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: _FakeClient()))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_market_radar_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"radar.add_watch", "radar.brief"}
    # Per modules.yaml there is NO gate for market-radar — neither capability is approval-gated.
    assert caps["radar.add_watch"]["approval_required"] is False
    assert caps["radar.brief"]["approval_required"] is False
    # add_watch creates a watch (side-effecting); brief is read-only.
    assert caps["radar.add_watch"]["side_effecting"] is True
    assert caps["radar.brief"]["side_effecting"] is False
    assert "watch" in caps["radar.add_watch"]["provides"]
    assert "market_brief" in caps["radar.brief"]["provides"]
    assert "market-radar:write" in caps["radar.add_watch"]["permissions"]


def test_invoke_add_watch_posts_to_changedetection(client):
    r = client.post("/invoke", json={
        "capability": "radar.add_watch",
        "inputs": {"url": "https://rival.example/prices", "title": "Rival Prices", "tag": "pricing"},
    }).json()
    res = r["result"]
    assert res["status"] == "done" and res["action"] == "add_watch"
    assert res["watch"]["uuid"] == "new-watch-1"
    assert res["watch"]["url"] == "https://rival.example/prices"
    # the REAL core POSTed a watch to changedetection's /api/v1/watch endpoint
    assert _FakeClient.posted == ["http://localhost:5001/api/v1/watch"]


def test_invoke_brief_is_readonly(client):
    res = client.post("/invoke", json={"capability": "radar.brief", "inputs": {}}).json()["result"]
    assert res["status"] == "done" and res["action"] == "brief"
    assert res["watches_reviewed"] == 1
    # the changed/unread pricing watch surfaces in the brief
    assert res["changed"] == 1
    assert _FakeClient.posted == []  # read-only: no writes to the core


def test_brief_computes_kpis_off_live_watches(client):
    # the operator's brief reads the same live activity the dashboard renders
    data = core.fetch_activity(force=True)
    assert data["core"] == "changedetection"
    assert data["counts"]["watches"] == 1 and data["counts"]["unread"] == 1


def test_idempotency_dedupes_add_watch(client):
    body = {
        "capability": "radar.add_watch",
        "inputs": {"url": "https://rival.example/prices", "title": "Rival Prices"},
        "idempotency_key": "k-1",
    }
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    assert _FakeClient.posted == ["http://localhost:5001/api/v1/watch"]  # POSTed exactly once


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"market-radar": "http://market-radar"}, transport=_transport)
    brief = oc.invoke("market-radar", "radar.brief", {}, idempotency_key="m-1")
    assert brief["status"] == "done" and brief["watches_reviewed"] == 1

    added = oc.invoke("market-radar", "radar.add_watch",
                      {"url": "https://new.example", "title": "New"}, idempotency_key="m-2")
    assert added["status"] == "done" and added["watch"]["uuid"] == "new-watch-1"
