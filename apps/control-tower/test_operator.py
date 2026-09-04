"""agentic-control-tower as a Mission Runtime operator — proof over a fake Metabase core.

Exercises the REAL core logic (fetch_activity KPIs + ask NL->template->query + refresh) and
the SDK-mount contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission
Runtime's own HTTPOperatorClient driving the operator over the wire with exactly-once
idempotency.

The package dir has a HYPHEN (`control-tower`), so it can't be imported with `import
control-tower`; we load it the way the intra-package code does — via importlib.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/control-tower/test_operator.py -q
"""
from __future__ import annotations

import importlib
import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

core = importlib.import_module("control-tower.core")
_operator = importlib.import_module("control-tower.operator")
build_control_tower_operator = _operator.build_control_tower_operator
from agentic_os.mission.operators import HTTPOperatorClient


# ── a fake Metabase core (routes /api/dataset by the SQL it receives) ─────────
def _cols(names):
    return [{"name": n} for n in names]


def _dataset(sql: str):
    """Return (cols, rows) for the pre-written template SQL the app sends."""
    if "AS backlog" in sql:                       # _q_kpi_extras
        return _cols(["margin_pct", "ar_outstanding", "conversion_pct", "backlog"]), \
            [[38, 25000.0, 62, 90000.0]]
    if "ORDER BY margin_pct DESC" in sql:         # _q_margin_by_service
        return _cols(["service_line", "revenue", "margin_pct"]), \
            [["Portfolio Management", 120000.0, 42], ["Financial Planning", 40000.0, 30]]
    if "AS month" in sql and "AS jobs" in sql and "AS revenue" in sql:  # _q_revenue_by_month
        return _cols(["month", "jobs", "revenue"]), \
            [["2026-06", 10, 50000.0], ["2026-07", 12, 60000.0]]
    if "service_type AS service_line" in sql:     # _q_revenue_by_service
        return _cols(["service_line", "jobs", "revenue"]), \
            [["Portfolio Management", 20, 120000.0], ["Financial Planning", 15, 40000.0]]
    return _cols([]), []


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeMetabase:
    """Stands in for the httpx module — records POSTs to /api/dataset (the only writes to talk to)."""
    dataset_posts: list[str] = []

    @classmethod
    def get(cls, url, headers=None, params=None, timeout=None):
        if url.endswith("/api/health"):
            return _Resp(200, {"status": "ok"})
        if url.endswith("/api/user/current"):
            return _Resp(200, {"id": 1})          # session valid → no re-login
        if url.endswith("/api/database"):
            return _Resp(200, {"data": []})
        return _Resp(200, {})

    @classmethod
    def post(cls, url, headers=None, json=None, timeout=None):
        if url.endswith("/api/dataset"):
            sql = ((json or {}).get("native") or {}).get("query", "")
            cls.dataset_posts.append(sql)
            cols, rows = _dataset(sql)
            return _Resp(200, {"status": "completed", "data": {"cols": cols, "rows": rows}})
        if url.endswith("/api/session"):
            return _Resp(200, {"id": "fresh-session"})
        return _Resp(200, {})


@pytest.fixture(autouse=True)
def _fake_metabase(monkeypatch):
    _FakeMetabase.dataset_posts = []
    core._CACHE.update(ts=0.0, data=None)         # no cache bleed between tests
    core._DB_ID_CACHE["id"] = None
    monkeypatch.setitem(core._session, "token", "test-session")
    monkeypatch.setattr(core, "METABASE_DB_NAME", "")   # skip /api/database → fallback id
    monkeypatch.setattr(core, "httpx", _FakeMetabase)
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_control_tower_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"bi.summary", "bi.ask", "bi.refresh"}
    # Control Tower is read-only analytics: per modules.yaml NO gate on any capability,
    # and nothing is side-effecting (a query only reads).
    for c in caps.values():
        assert c["approval_required"] is False
        assert c["side_effecting"] is False
        assert "bi:read" in c["permissions"]
    assert "bi_activity" in caps["bi.summary"]["provides"]
    assert "bi_answer" in caps["bi.ask"]["provides"]
    assert "bi_activity" in caps["bi.refresh"]["provides"]


def test_invoke_summary_reads_kpis(client):
    res = client.post("/invoke", json={"capability": "bi.summary", "inputs": {}}).json()["result"]
    assert res["core"] == "metabase" and res["connected"] is True
    assert res["counts"]["months"] == 2          # two months from the fake revenue-by-month
    assert res["counts"]["service_lines"] == 2
    # the REAL core ran the three dashboard queries against the (fake) Metabase /api/dataset
    assert len(_FakeMetabase.dataset_posts) == 3


def test_invoke_ask_routes_to_template_and_queries(client):
    r = client.post("/invoke", json={
        "capability": "bi.ask",
        "inputs": {"question": "which service line makes the most margin?"},
    }).json()["result"]
    assert r["status"] == "done" and r["action"] == "ask"
    # deterministic keyword routing (no LLM in the operator path) picked the margin template,
    assert r["matched_report"] == "margin_by_service"
    assert r["chart"] == "bar" and r["rows"]
    assert r["answer"].startswith("Top service line: Portfolio Management")
    # and only the pre-written template SQL ran (never model-authored SQL).
    assert _FakeMetabase.dataset_posts == [core._q_margin_by_service()]


def test_invoke_refresh_reruns_dashboard(client):
    res = client.post("/invoke", json={"capability": "bi.refresh", "inputs": {}}).json()["result"]
    assert res["status"] == "done" and res["action"] == "refresh"
    assert res["months"] == 2 and res["service_lines"] == 2
    assert len(_FakeMetabase.dataset_posts) == 3


def test_idempotency_dedupes_repeat_invoke(client):
    # Control Tower has no side-effecting capability, so exercise exactly-once on a read:
    # a second invoke with the same key returns the first result WITHOUT re-running queries.
    body = {"capability": "bi.refresh", "inputs": {}, "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    assert len(_FakeMetabase.dataset_posts) == 3     # queried once, not twice


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"control-tower": "http://control-tower"}, transport=_transport)
    summary = oc.invoke("control-tower", "bi.summary", {}, idempotency_key="m-1")
    assert summary["core"] == "metabase" and summary["counts"]["months"] == 2

    answer = oc.invoke("control-tower", "bi.ask",
                       {"question": "where are my best leads coming from?"}, idempotency_key="m-2")
    assert answer["status"] == "done" and answer["matched_report"] == "lead_source"
