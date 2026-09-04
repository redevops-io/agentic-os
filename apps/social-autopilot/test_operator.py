"""agentic-social-autopilot as a Mission Runtime operator — proof over a fake Postiz core.

Exercises the REAL core logic (fetch_activity KPIs + draft staging + publish gating) and the
SDK-mount contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's
own HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

Boundary note: unlike agentic-billing (whose Lago core is REST/httpx), social-autopilot reads
and writes its Postiz core over **postgres via the `psql` client** — Postiz's REST API doesn't
bind its port in this stack (see app.py docstring). So the fake here intercepts that psql
subprocess boundary (`core.subprocess.run`); the REAL `_psql` / `fetch_activity` / `draft` /
`publish` logic runs against the fake Postiz store — the faithful analog of billing's httpx fake.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/social-autopilot/test_operator.py -q

The package dir has a HYPHEN, so it is loaded via importlib.import_module("social-autopilot.*").
"""
from __future__ import annotations

import importlib
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

core = importlib.import_module("social-autopilot.core")
build_social_operator = importlib.import_module("social-autopilot.operator").build_social_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake Postiz core (one channel + one queued post) ───────────────────────
_CHANNEL_ROW = "int-1\x1fMeridian Instagram\x1finstagram\x1f{\"followers\": 1200}\x1ff"
_POST_ROW = ("post-1\x1fQUEUE\x1f2026-07-15 10:00:00\x1fint-1\x1f"
             "[{\"content\": \"Q3 market commentary is live — read our take. Not investment advice. #MarketCommentary\"}]")


class _Done:
    """Stands in for subprocess.CompletedProcess."""
    def __init__(self, out=""):
        self.returncode = 0
        self.stdout = out
        self.stderr = ""


class _FakePsql:
    """Stands in for subprocess.run — answers the Postiz reads, records the INSERT writes."""
    inserts: list[str] = []
    queries: list[str] = []

    @classmethod
    def run(cls, args, text=None, capture_output=None, timeout=None, env=None):
        sql = args[-1]
        cls.queries.append(sql)
        if 'INSERT INTO "Post"' in sql:
            cls.inserts.append(sql)
            return _Done("")
        if 'FROM "Post"' in sql:
            return _Done(_POST_ROW + "\n")
        if 'FROM "Integration"' in sql and "LIMIT 1" in sql:  # draft's first-channel lookup
            return _Done("int-1\x1finstagram\n")
        if 'FROM "Integration"' in sql:                       # fetch_activity channels
            return _Done(_CHANNEL_ROW + "\n")
        return _Done("1\n")                                   # SELECT 1 health probe


@pytest.fixture(autouse=True)
def _fake_postiz(monkeypatch):
    _FakePsql.inserts = []
    _FakePsql.queries = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core.subprocess, "run", _FakePsql.run)
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_social_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"social.draft", "social.publish"}
    assert m["operator"] == "social-autopilot"
    # per modules.yaml `approval_required:[publish]` — only publishing to networks is gated
    assert caps["social.publish"]["approval_required"] is True
    assert caps["social.draft"]["approval_required"] is False
    # both are real writes to the Postiz core (draft persists a DRAFT; publish is the syscall)
    assert caps["social.draft"]["side_effecting"] is True
    assert caps["social.publish"]["side_effecting"] is True
    assert "draft_staged" in caps["social.draft"]["provides"]
    assert "publish_staged" in caps["social.publish"]["provides"]
    assert "social:write" in caps["social.publish"]["permissions"]


def test_invoke_draft_stages_to_postiz(client):
    r = client.post("/invoke", json={
        "capability": "social.draft", "inputs": {"topic": "tax-loss harvesting"},
    }).json()
    res = r["result"]
    assert res["status"] == "done" and res["action"] == "draft"
    assert res["staged_as_draft"] is True
    assert res["network"] == "Instagram"           # _net_label('instagram')
    assert (res["post_id"] or "").startswith("draft-")
    assert res["copy_source"] == "template"         # operator path injects no LLM callback
    # the REAL core INSERTed exactly one DRAFT row into the (fake) Postiz Post table
    assert len(_FakePsql.inserts) == 1
    assert "'DRAFT'" in _FakePsql.inserts[0]


def test_invoke_publish_is_gated_and_writes_nothing(client):
    r = client.post("/invoke", json={"capability": "social.publish", "inputs": {}}).json()
    res = r["result"]
    assert res["status"] == "pending_approval" and res["action"] == "publish"
    assert res["requires"] == "human approval"
    assert res["id"] == "post-1"                    # picks the head of the live Postiz queue
    assert _FakePsql.inserts == []                  # nothing is published / written


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "social.draft", "inputs": {"topic": "retirement income"},
            "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second                          # exactly-once: same result echoed
    assert len(_FakePsql.inserts) == 1              # ...and only ONE DRAFT actually written


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"social-autopilot": "http://social"}, transport=_transport)
    drafted = oc.invoke("social-autopilot", "social.draft", {"topic": "market outlook"},
                        idempotency_key="m-1")
    assert drafted["status"] == "done" and drafted["staged_as_draft"] is True

    pub = oc.invoke("social-autopilot", "social.publish", {}, idempotency_key="m-2")
    assert pub["status"] == "pending_approval" and pub["id"] == "post-1"
