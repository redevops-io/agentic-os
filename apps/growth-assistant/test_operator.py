"""growth-assistant as a Mission Runtime operator — proof over fake cores.

Exercises the REAL core action logic (deterministic template path, gen=None) and the SDK-mount
contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's own
HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

The package dir has a HYPHEN, so it's imported via importlib (never `import growth-assistant`).
Cores (ERPNext / Listmonk / Postiz) are faked at the httpx boundary; the asset store writes to
a tmp dir. No LLM is involved — the operator drives the core with gen=None, so every capability
is deterministic and exercisable offline.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/growth-assistant/test_operator.py -q
"""
from __future__ import annotations

import importlib
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

core = importlib.import_module("growth-assistant.core")
operator_mod = importlib.import_module("growth-assistant.operator")
build_growth_assistant_operator = operator_mod.build_growth_assistant_operator

from agentic_os.mission.operators import HTTPOperatorClient  # noqa: E402


# ── a fake httpx bound to the three cores (records writes) ────────────────────
class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}

    def json(self):
        return self._p


class _FakeClient:
    """Stands in for httpx.Client — records ERPNext Lead / Listmonk list POSTs."""

    def __init__(self, http):
        self._http = http

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self._http.posts.append(url)
        if url.endswith("/api/resource/Lead"):
            return _Resp(201, {"data": {"name": "CRM-LEAD-001"}})
        if url.endswith("/api/lists"):
            return _Resp(201, {"data": {"id": 42}})
        return _Resp(200, {})


class _FakeHTTP:
    """Records every POST url (module-level + Client) for assertions; all reads return 200."""

    def __init__(self):
        self.posts: list[str] = []

    def get(self, url, headers=None, params=None, timeout=None):
        return _Resp(200)

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(url)
        return _Resp(200, {})

    def Client(self, timeout=None):
        return _FakeClient(self)


@pytest.fixture(autouse=True)
def _fake_cores(monkeypatch, tmp_path):
    http = _FakeHTTP()
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "httpx", http)
    monkeypatch.setattr(core, "ASSET_DIR", tmp_path)
    monkeypatch.setattr(core, "GENERATION_ONLY", False)
    # give the cores credentials + endpoints so the push paths actually run
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "erp-key")
    monkeypatch.setattr(core, "ERPNEXT_API_SECRET", "erp-secret")
    monkeypatch.setattr(core, "LISTMONK_API_TOKEN", "lm-token")
    monkeypatch.setattr(core, "POSTIZ_API_URL", "http://postiz")
    monkeypatch.setattr(core, "POSTIZ_API_KEY", "postiz-key")
    yield http


@pytest.fixture()
def http(_fake_cores):
    return _fake_cores


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_growth_assistant_operator().router())
    return TestClient(app)


def _invoke(client, capability, inputs=None, idem=""):
    body = {"capability": capability, "inputs": inputs or {}}
    if idem:
        body["idempotency_key"] = idem
    return client.post("/invoke", json=body).json()["result"]


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    assert m["operator"] == "growth-assistant"
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {
        "assistant.playbook", "assistant.subreddit_plan", "assistant.founder_content",
        "assistant.community_blueprint", "assistant.cold_outreach", "assistant.hire_brief",
        "assistant.ask",
    }
    # the three push-capable capabilities are approval-gated (generation free, PUSH gated)
    for n in ("founder_content", "community_blueprint", "cold_outreach"):
        assert caps[f"assistant.{n}"]["approval_required"] is True
    for n in ("playbook", "subreddit_plan", "hire_brief", "ask"):
        assert caps[f"assistant.{n}"]["approval_required"] is False
    # core-writing capabilities AND local-asset writers (playbook/subreddit_plan/hire_brief) are side-effecting
    assert caps["assistant.founder_content"]["side_effecting"] is True
    assert caps["assistant.community_blueprint"]["side_effecting"] is True
    assert caps["assistant.cold_outreach"]["side_effecting"] is True
    assert caps["assistant.playbook"]["side_effecting"] is True      # writes an asset JSON
    assert caps["assistant.subreddit_plan"]["side_effecting"] is True
    assert caps["assistant.hire_brief"]["side_effecting"] is True
    assert caps["assistant.ask"]["side_effecting"] is False
    # provides wiring is present for the planner to reason over
    assert "growth_playbook" in caps["assistant.playbook"]["provides"]
    assert "erpnext:write" in caps["assistant.cold_outreach"]["permissions"]


def test_playbook_is_asset_only_no_core_write(client, http):
    res = _invoke(client, "assistant.playbook", {"startup": {"name": "CIWatch", "problem": "flaky CI"}})
    assert res["status"] == "done" and res["action"] == "playbook"
    assert res["asset_id"] and isinstance(res["playbook"], dict)
    assert res["playbook"]["north_star_metric"]
    assert http.posts == []  # template path: no writes to any core


def test_subreddit_plan_runs_template(client, http):
    res = _invoke(client, "assistant.subreddit_plan", {"startup": {"name": "CIWatch"}})
    assert res["status"] == "done" and res["asset_id"]
    assert len(res["plan"]["first_100_threads"]) >= 1
    assert http.posts == []


def _approved(cap, inputs):
    """Trusted in-process path: the runtime delivers `_approval` on a resumed, approved node."""
    return build_growth_assistant_operator().invoke(cap, {**inputs, "_approval": {"approved": True}})


PUSH_CASES = {
    "assistant.cold_outreach": {"startup": {"name": "CIWatch"}, "push": True,
                                "targets": [{"name": "Jane Founder", "handle": "@jane", "company": "Acme"}]},
    "assistant.community_blueprint": {"startup": {"name": "CIWatch"}, "platform": "discord", "push": True},
    "assistant.founder_content": {"startup": {"name": "CIWatch"}, "platform": "x", "count": 3, "push": True},
}


@pytest.mark.parametrize("cap", sorted(PUSH_CASES))
def test_push_is_staged_without_approval_but_generation_still_runs(client, http, cap):
    op = build_growth_assistant_operator()
    results = [op.invoke(cap, dict(PUSH_CASES[cap]))]
    for extra in ({}, {"_approval": {"approved": True}}):      # open HTTP strips forged approval markers
        results.append(_invoke(client, cap, {**PUSH_CASES[cap], **extra}))
    for res in results:
        assert res["status"] == "done" and res["asset_id"]      # generation is free
        assert res["push_status"] == "pending_approval" and res["push_executed"] is False
        assert res["pushed"] == {}
    assert http.posts == []                                      # no core was written


@pytest.mark.parametrize("cap", sorted(PUSH_CASES))
def test_push_executes_with_runtime_approval(http, cap):
    res = _approved(cap, PUSH_CASES[cap])
    assert res["pushed"] and http.posts


def test_cold_outreach_pushes_erpnext_leads(client, http):
    res = _approved("assistant.cold_outreach", {
        "startup": {"name": "CIWatch"}, "push": True,
        "targets": [{"name": "Jane Founder", "handle": "@jane", "company": "Acme"}],
    })
    assert res["status"] == "done"
    assert res["pushed"]["erpnext"]["leads_created"] == ["CRM-LEAD-001"]
    assert http.posts == ["http://localhost:8092/api/resource/Lead"]


def test_community_blueprint_pushes_listmonk_list(client, http):
    res = _approved("assistant.community_blueprint", {
        "startup": {"name": "CIWatch"}, "platform": "discord", "push": True,
    })
    assert res["status"] == "done"
    assert res["pushed"]["listmonk"] == {"ok": True, "listmonk_list_id": 42}
    assert http.posts == ["http://localhost:9000/api/lists"]


def test_founder_content_pushes_postiz_drafts(client, http):
    res = _approved("assistant.founder_content", {
        "startup": {"name": "CIWatch"}, "platform": "x", "count": 3, "push": True,
    })
    assert res["status"] == "done" and res["platform"] == "x"
    assert len(res["content"]["posts"]) == 3
    assert res["pushed"]["postiz"]["drafts_created"] == 3
    assert http.posts == ["http://postiz/public/v1/posts"] * 3


def test_hire_brief_is_deterministic_no_core_write(client, http):
    res = _invoke(client, "assistant.hire_brief", {"role": "copywriter", "startup": {"name": "CIWatch"}})
    assert res["status"] == "done"
    assert "Upwork" in res["search_links"] and res["vetting_scorecard"]
    assert http.posts == []  # freelancer links are ToS-safe URLs, not API calls


def test_ask_is_readonly(client, http):
    res = _invoke(client, "assistant.ask", {"q": "how many assets do we have?"})
    assert res["status"] == "done" and res["q"]
    assert res["answer"]
    assert http.posts == []  # read-only Q&A: no writes


def test_idempotency_dedupes_side_effect(client, http):
    op = build_growth_assistant_operator()
    body = {"startup": {"name": "CIWatch"}, "platform": "discord", "push": True, "_approval": {"approved": True}}
    first = op.invoke("assistant.community_blueprint", body, "k-1")
    second = op.invoke("assistant.community_blueprint", body, "k-1")
    assert first == second
    assert http.posts == ["http://localhost:9000/api/lists"]  # list created exactly once


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"growth-assistant": "http://growth"}, transport=_transport)
    res = oc.invoke("growth-assistant", "assistant.playbook",
                    {"startup": {"name": "CIWatch"}}, idempotency_key="m-1")
    assert res["status"] == "done" and res["asset_id"]

    ask = oc.invoke("growth-assistant", "assistant.ask", {"q": "status?"}, idempotency_key="m-2")
    assert ask["status"] == "done" and ask["answer"]
