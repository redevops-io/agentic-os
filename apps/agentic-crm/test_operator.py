"""agentic-crm as a Mission Runtime operator — proof over a fake ERPNext CRM core.

Exercises the REAL core logic (score_lead / research / draft_outreach / qualify against the
ERPNext REST doctypes) and the SDK-mount contract (GET /capabilities + POST /invoke)
end-to-end, plus the Mission Runtime's own HTTPOperatorClient driving the operator over the
wire with exactly-once idempotency.

The package dir is `agentic-crm` (HYPHEN), so `from agentic-crm import core` is invalid
Python — import via importlib.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/agentic-crm/test_operator.py -q
"""
from __future__ import annotations

import importlib
import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Hyphenated package dir → importlib (a plain `import` statement can't spell it).
core = importlib.import_module("agentic-crm.core")
build_crm_operator = importlib.import_module("agentic-crm.operator").build_crm_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake ERPNext CRM core (one open lead + one open opportunity) ───────────
LEAD = {
    "name": "CRM-LEAD-0001", "lead_name": "Dana Rivera", "company_name": "Whitfield Family Trust",
    "status": "Open", "source": "Website", "email_id": "dana@whitfield.example",
    "designation": "Trustee", "industry": "Private Wealth", "territory": "West",
    "creation": "2026-06-01 09:00:00",
}
OPP = {
    "name": "OPP-0001", "party_name": "Whitfield Family Trust", "customer_name": "Whitfield Family Trust",
    "status": "Open", "opportunity_amount": 42000, "sales_stage": "Prospecting",
    "creation": "2026-06-02 09:00:00",
}
CUSTOMER = {"name": "Okonkwo Holdings"}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_get(url, timeout=None, headers=None, params=None):
    return _Resp(200)  # erp_connected() probe → /api/resource/Lead


class _FakeClient:
    """Stands in for httpx.Client — records Comment POSTs (writes) and field PUTs."""
    posted: list[str] = []
    put_calls: list[str] = []
    comment_bodies: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        # _get_doc: single record → /api/resource/Lead/<name>
        if "/api/resource/Lead/" in url:
            return _Resp(200, {"data": LEAD})
        if url.endswith("/api/resource/Lead"):
            return _Resp(200, {"data": [LEAD]})
        if url.endswith("/api/resource/Opportunity"):
            return _Resp(200, {"data": [OPP]})
        if url.endswith("/api/resource/Customer"):
            return _Resp(200, {"data": [CUSTOMER]})
        return _Resp(200, {"data": []})

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append(url)
        _FakeClient.comment_bodies.append((json or {}).get('content', ''))
        return _Resp(200, {})  # Comment created

    def put(self, url, headers=None, json=None):
        _FakeClient.put_calls.append(url)
        return _Resp(200, {})  # field set


@pytest.fixture(autouse=True)
def _fake_erpnext(monkeypatch):
    _FakeClient.posted = []
    _FakeClient.put_calls = []
    _FakeClient.comment_bodies = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "test-key")
    monkeypatch.setattr(core, "ERPNEXT_API_SECRET", "test-secret")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, post=_fake_get,
                                              Client=lambda timeout=None: _FakeClient()))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_crm_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    assert m["operator"] == "agentic-crm"
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"crm.score_lead", "crm.research", "crm.draft_outreach", "crm.qualify"}
    # modules.yaml gate is [send] — the human sends outreach. draft_outreach only DRAFTS
    # (saves a Comment; nothing here reaches a prospect), so NO capability is gated.
    for name in caps:
        assert caps[name]["approval_required"] is False, name
    # every action writes to ERPNext → all side-effecting (runtime dedupes exactly-once).
    for name in caps:
        assert caps[name]["side_effecting"] is True, name
        assert "crm:write" in caps[name]["permissions"], name
    assert "lead_score" in caps["crm.score_lead"]["provides"]
    assert "lead_qualified" in caps["crm.qualify"]["provides"]


def test_invoke_score_lead_writes_comment(client):
    r = client.post("/invoke", json={
        "capability": "crm.score_lead", "inputs": {"lead": "CRM-LEAD-0001"},
    }).json()
    res = r["result"]
    assert res["status"] == "done" and res["action"] == "score_lead"
    assert res["lead"] == "CRM-LEAD-0001"
    # the REAL core POSTed an auditable Comment to ERPNext for the scored lead.
    assert _FakeClient.posted == ["http://localhost:8092/api/resource/Comment"]
    # no LLM injected by the operator → deterministic rules score, labelled, written as an int 0-100.
    assert res["method"] == "rules" and res["model"] == "rules"
    assert isinstance(res["score"], int) and 0 <= res["score"] <= 100
    assert len(_FakeClient.put_calls) == 1 and "/Lead/CRM-LEAD-0001" in _FakeClient.put_calls[0]
    assert "None/100" not in str(_FakeClient.comment_bodies) and "DeepSeek" not in str(_FakeClient.comment_bodies)


def test_invoke_research_writes_comment(client):
    res = client.post("/invoke", json={
        "capability": "crm.research", "inputs": {"lead": "CRM-LEAD-0001"},
    }).json()["result"]
    assert res["status"] == "llm_unavailable" and res["action"] == "research"   # no LLM: nothing written
    assert _FakeClient.posted == []
    res = core.research({"lead": "CRM-LEAD-0001"}, llm=lambda p, n=400: "A brief.")
    assert res["status"] == "done" and res["web_enriched"] is False  # DEERFLOW_URL unset
    assert _FakeClient.posted == ["http://localhost:8092/api/resource/Comment"]


def test_invoke_draft_outreach_only_drafts(client):
    res = client.post("/invoke", json={
        "capability": "crm.draft_outreach", "inputs": {"lead": "CRM-LEAD-0001"},
    }).json()["result"]
    assert res["status"] == "llm_unavailable" and _FakeClient.posted == []   # never writes "(brain unavailable)"
    res = core.draft_outreach({"lead": "CRM-LEAD-0001"}, llm=lambda p, n=300: "Subject: hi\n\nbody")
    assert res["status"] == "done" and res["action"] == "draft_outreach"
    # draft is staged as a Lead Comment for a human — never auto-sent to the prospect.
    assert res["posted_as"] == "Lead comment"
    assert res["requires"] == "human approval to send"
    assert _FakeClient.posted == ["http://localhost:8092/api/resource/Comment"]


def test_invoke_qualify_sets_status(client):
    res = client.post("/invoke", json={
        "capability": "crm.qualify", "inputs": {"lead": "CRM-LEAD-0001", "status": "Replied"},
    }).json()["result"]
    assert res["status"] == "done" and res["action"] == "qualify"
    assert res["new_status"] == "Replied"
    # the REAL core PUT the status field and POSTed an audit Comment.
    assert len(_FakeClient.put_calls) == 1
    assert _FakeClient.put_calls[0].startswith("http://localhost:8092/api/resource/Lead/")
    assert _FakeClient.posted == ["http://localhost:8092/api/resource/Comment"]


def test_unknown_lead_is_an_error(client):
    """When ERPNext has no such Lead, the core returns a structured error, no writes."""
    def _no_doc(self, url, headers=None, params=None):
        return _Resp(404, {})  # _get_doc → not found
    # patch just the single-record GET to miss
    orig_get = _FakeClient.get
    _FakeClient.get = lambda self, url, headers=None, params=None: (
        _Resp(404, {}) if "/api/resource/Lead/" in url else orig_get(self, url, headers, params))
    try:
        res = client.post("/invoke", json={
            "capability": "crm.qualify", "inputs": {"lead": "MISSING"},
        }).json()["result"]
    finally:
        _FakeClient.get = orig_get
    assert res["status"] == "error"
    assert _FakeClient.put_calls == [] and _FakeClient.posted == []


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "crm.score_lead", "inputs": {"lead": "CRM-LEAD-0001"},
            "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    # exactly-once: the side-effecting Comment POST happened a single time.
    assert _FakeClient.posted == ["http://localhost:8092/api/resource/Comment"]


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"agentic-crm": "http://agentic-crm"}, transport=_transport)
    res = oc.invoke("agentic-crm", "crm.qualify",
                    {"lead": "CRM-LEAD-0001", "status": "Opportunity"}, idempotency_key="m-1")
    assert res["status"] == "done" and res["new_status"] == "Opportunity"

    # idempotency key flows through the HTTP surface → second call dedupes.
    again = oc.invoke("agentic-crm", "crm.qualify",
                      {"lead": "CRM-LEAD-0001", "status": "Opportunity"}, idempotency_key="m-1")
    assert again == res
    assert len(_FakeClient.put_calls) == 1  # exactly-once across the two wire calls


# ── score_lead: validation, rules fallback, LLM path ─────────────────────────
def _llm(text):
    return lambda prompt, max_tokens=400: text


def test_score_clamped_and_labelled_llm():
    res = core.score_lead({"lead": "CRM-LEAD-0001"}, llm=_llm('{"score": 250, "rationale": "r", "next_action": "n"}'),
                          model="my-model-7b")
    assert res["method"] == "llm" and res["score"] == 100 and res["model"] == "my-model-7b"
    body = _FakeClient.comment_bodies[-1]
    assert "100/100 (method: llm)" in body and "my-model-7b" in body and "DeepSeek" not in body


@pytest.mark.parametrize("bad", ['{"score": null}', '{"score": "high"}', "not json", '{"score": true}', ""])
def test_invalid_llm_score_never_written_when_method_llm(bad):
    res = core.score_lead({"lead": "CRM-LEAD-0001", "method": "llm"}, llm=_llm(bad))
    assert res["status"] == "llm_unavailable" and res["score"] is None
    assert _FakeClient.posted == [] and _FakeClient.put_calls == []


def test_method_llm_without_llm_writes_nothing():
    res = core.score_lead({"lead": "CRM-LEAD-0001", "method": "llm"})
    assert res["status"] == "llm_unavailable" and _FakeClient.posted == [] and _FakeClient.put_calls == []


def test_auto_falls_back_to_rules_on_invalid_llm():
    res = core.score_lead({"lead": "CRM-LEAD-0001"}, llm=_llm("garbage"))
    assert res["status"] == "done" and res["method"] == "rules" and isinstance(res["score"], int)


def test_rule_score_features_and_range():
    base = {"source": "Referral", "no_of_employees": "201-500", "status": "Replied",
            "modified": "2999-01-01 00:00:00"}
    hi = core.rule_score(base)["score"]
    lo = core.rule_score({"source": "Cold Calling", "no_of_employees": "1-10", "status": "Open",
                          "modified": "2001-01-01 00:00:00"})["score"]
    assert 0 <= lo < hi <= 100
    assert hi == 100 and core.rule_score({})["score"] == 10 + 8 + 5
    assert core.rule_score({"source": "Referral"})["score"] > core.rule_score({"source": "Cold Calling"})["score"]


def test_manifest_declares_no_phantom_send_approval():
    manifest = importlib.import_module("agentic-crm.manifest").MANIFEST
    assert "send" not in tuple(manifest.deploy.approval)
