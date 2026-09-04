"""agentic-growth-engine as a Mission Runtime operator — proof over a fake Umami core.

Exercises the REAL core logic (fetch_activity attribution + analyze recommendation and
the reallocate_budget staging gate) and the SDK-mount contract (GET /capabilities +
POST /invoke) end-to-end, plus the Mission Runtime's own HTTPOperatorClient driving the
operator over the wire with exactly-once idempotency.

The package dir has a HYPHEN ("growth-engine"), so it is imported via importlib rather
than a dotted `import` statement.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/growth-engine/test_operator.py -q
"""
from __future__ import annotations

import importlib
import types
from urllib.parse import urlparse, parse_qs

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

core = importlib.import_module("growth-engine.core")
operator = importlib.import_module("growth-engine.operator")
build_growth_operator = operator.build_growth_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake Umami core (two paid sources → a real shift recommendation) ────────
STATS = {"pageviews": 1000, "visitors": 400, "visits": 500, "bounces": 150}
UTM_SOURCES = [{"x": "google", "y": 120}, {"x": "linkedin", "y": 80}]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeUmami:
    """Stands in for the httpx module — records every GET/POST the core makes."""
    gets: list[str] = []
    posts: list[str] = []

    @classmethod
    def get(cls, url, headers=None, params=None, timeout=None):
        cls.gets.append(url)
        if url.endswith("/api/heartbeat"):
            return _Resp(200, {"ok": True})
        if url.endswith("/stats"):
            return _Resp(200, STATS)
        if url.endswith("/metrics"):
            mtype = (parse_qs(urlparse(url).query).get("type") or [None])[0]
            if mtype is None and params:
                mtype = params.get("type")
            return _Resp(200, UTM_SOURCES if mtype == "utmSource" else [])
        return _Resp(200, {})

    @classmethod
    def post(cls, url, json=None, timeout=None):
        cls.posts.append(url)
        if url.endswith("/api/auth/login"):
            return _Resp(200, {"token": "fake-token"})
        return _Resp(200, {})


@pytest.fixture(autouse=True)
def _fake_umami(monkeypatch):
    _FakeUmami.gets = []
    _FakeUmami.posts = []
    core._CACHE.update(ts=0.0, data=None)      # no cache bleed between tests
    core._TOKEN.update(value=None, ts=0.0)     # force a fresh login each test
    monkeypatch.setattr(core, "WEBSITE_ID", "site-1")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_FakeUmami.get, post=_FakeUmami.post))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_growth_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"growth.analyze", "growth.reallocate_budget"}
    # the budget-moving capability is gated (matches modules.yaml's [budget_change] gate);
    # attribution/analyze reads are not.
    assert caps["growth.reallocate_budget"]["approval_required"] is True
    assert caps["growth.reallocate_budget"]["side_effecting"] is True
    assert "growth:write" in caps["growth.reallocate_budget"]["permissions"]
    assert "budget_change_staged" in caps["growth.reallocate_budget"]["provides"]
    assert caps["growth.analyze"]["approval_required"] is False
    assert caps["growth.analyze"]["side_effecting"] is False
    assert "growth_attribution" in caps["growth.analyze"]["provides"]


def test_invoke_analyze_reads_umami(client):
    res = client.post("/invoke", json={"capability": "growth.analyze", "inputs": {}}).json()["result"]
    assert res["status"] == "done" and res["action"] == "analyze"
    # attribution computed from the REAL core over the fake Umami traffic
    labels = {f["channel"] for f in res["findings"]}
    assert "Google Ads" in labels and "LinkedIn" in labels
    # two paid channels with different ROAS → a concrete shift recommendation
    assert res["recommendation"] and "reallocate_budget" in res["recommendation"]
    assert "Umami" in res["source"]
    # read-only: it hit the Umami read endpoints, never staged a budget change
    assert any(u.endswith("/stats") for u in _FakeUmami.gets)


def test_invoke_reallocate_budget_stages(client):
    r = client.post("/invoke", json={
        "capability": "growth.reallocate_budget",
        "inputs": {"from": "linkedin", "to": "google", "amount": 600},
    }).json()
    res = r["result"]
    assert res["status"] == "pending_approval" and res["action"] == "reallocate_budget"
    assert res["approval_required"] == "budget_change"
    assert res["from"] == "linkedin" and res["to"] == "google" and res["amount"] == 600
    # staging is pure: it touches NO Umami endpoint (ad spend lives in the Ads platform)
    assert _FakeUmami.gets == [] and _FakeUmami.posts == []


def test_idempotency_dedupes_side_effect(client, monkeypatch):
    calls = {"n": 0}
    real = core.reallocate_budget

    def _counting(body):
        calls["n"] += 1
        return real(body)

    monkeypatch.setattr(core, "reallocate_budget", _counting)
    op = build_growth_operator()  # fresh operator bound to the patched core
    app = FastAPI()
    app.include_router(op.router())
    c = TestClient(app)

    body = {"capability": "growth.reallocate_budget",
            "inputs": {"from": "linkedin", "to": "google", "amount": 600},
            "idempotency_key": "k-1"}
    first = c.post("/invoke", json=body).json()["result"]
    second = c.post("/invoke", json=body).json()["result"]
    assert first == second
    assert calls["n"] == 1  # exactly-once: the second call is served from the dedupe cache


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"growth-engine": "http://growth-engine"}, transport=_transport)
    res = oc.invoke("growth-engine", "growth.analyze", {}, idempotency_key="m-1")
    assert res["status"] == "done" and res["action"] == "analyze"

    staged = oc.invoke("growth-engine", "growth.reallocate_budget",
                       {"from": "linkedin", "to": "google", "amount": 600}, idempotency_key="m-2")
    assert staged["status"] == "pending_approval" and staged["approval_required"] == "budget_change"
