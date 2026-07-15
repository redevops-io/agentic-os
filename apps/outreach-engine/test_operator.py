"""agentic-outreach-engine as a Mission Runtime operator — proof over a fake Twenty CRM core.

Exercises the REAL core logic (pipeline build + Twenty pilot sync) and the SDK-mount contract
(GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's own HTTPOperatorClient
driving the operator over the wire with exactly-once idempotency.

The package dir has a HYPHEN (`outreach-engine`) so `from outreach-engine import ...` is invalid
Python — the modules are loaded via importlib. The whole core talks to Twenty (and the GitHub/HN
signal sources) through `urllib.request.urlopen`, so the fake is installed at that one boundary.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/outreach-engine/test_operator.py -q
"""
from __future__ import annotations

import importlib
import json
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# hyphenated package dir → importlib, not a bare `import` statement
core = importlib.import_module("outreach-engine.core")
operator = importlib.import_module("outreach-engine.operator")
from agentic_os.mission.operators import HTTPOperatorClient


# ── a fake Twenty CRM + GitHub/HN, all behind urllib.request.urlopen ─────────
class _Twenty:
    """Records the write calls the REAL core makes to Twenty's REST API."""
    posts: list[str] = []


class _Resp:
    """Stands in for the urllib response object (context-manager + .read())."""
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    url = getattr(req, "full_url", None) or str(req)
    method = req.get_method() if hasattr(req, "get_method") else "GET"
    # Twenty writes (the side effect twenty_sync performs)
    if "/rest/companies" in url:
        _Twenty.posts.append(url)
        return _Resp({"data": {"createCompany": {"id": "co-1"}}})
    if "/rest/people" in url:
        _Twenty.posts.append(url)
        return _Resp({"data": {"createPerson": {"id": "pe-1"}}})
    if "/rest/opportunities" in url and method == "POST":
        _Twenty.posts.append(url)
        return _Resp({"data": {"createOpportunity": {"id": "op-1"}}})
    # public signal sources — empty so build_pipeline falls back to the offline DEMO set
    if "api.github.com" in url:
        return _Resp({"items": []})
    if "hn.algolia.com" in url:
        return _Resp({"hits": []})
    return _Resp({})


@pytest.fixture(autouse=True)
def _fake_twenty(monkeypatch):
    _Twenty.posts = []
    core._STATE.update(accounts=[], approved=set())     # no state bleed between tests
    core._LIVE_CACHE.update(ts=0.0, rows=[])            # no cached live signals
    monkeypatch.setattr(core, "TWENTY_API_KEY", "test-key")
    monkeypatch.setattr(core.urllib.request, "urlopen", _fake_urlopen)
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(operator.build_outreach_operator().router())
    return TestClient(app)


def _result(client, capability, inputs=None, idempotency_key=""):
    return client.post("/invoke", json={
        "capability": capability, "inputs": inputs or {}, "idempotency_key": idempotency_key,
    }).json()["result"]


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    assert m["operator"] == "outreach-engine"
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"outreach.refresh", "outreach.approve", "outreach.send_all"}
    # the gate is modules.yaml `approval_required: [send_sequence]` — only the send is gated
    assert caps["outreach.send_all"]["approval_required"] is True
    assert caps["outreach.refresh"]["approval_required"] is False
    assert caps["outreach.approve"]["approval_required"] is False
    # side_effecting = writes/sends: refresh only reads, approve + send_all mutate the world
    assert caps["outreach.refresh"]["side_effecting"] is False
    assert caps["outreach.approve"]["side_effecting"] is True
    assert caps["outreach.send_all"]["side_effecting"] is True
    assert "outreach_pipeline" in caps["outreach.refresh"]["provides"]
    assert "lead_synced" in caps["outreach.approve"]["provides"]
    assert "sequences_sent" in caps["outreach.send_all"]["provides"]
    assert "outreach:write" in caps["outreach.approve"]["permissions"]
    assert "outreach:write" in caps["outreach.send_all"]["permissions"]


def test_invoke_refresh_builds_pipeline_no_write(client):
    res = _result(client, "outreach.refresh", {})
    assert res["ok"] is True and res["action"] == "refresh"
    assert res["n"] == len(core.DEMO)          # offline signal sources → DEMO leads ranked
    assert _Twenty.posts == []                 # read-only: nothing written to Twenty


def test_invoke_approve_syncs_pilot_to_twenty(client):
    _result(client, "outreach.refresh", {})    # populate the ranked pipeline first
    acct = core.DEMO[0]["account"]
    res = _result(client, "outreach.approve", {"account": acct})
    assert res["ok"] is True and res["action"] == "approve" and res["account"] == acct
    assert res["twenty_synced"] is True
    assert acct in core._STATE["approved"]
    # the REAL core POSTed a Company + Opportunity to Twenty (manual lead → no maintainer Person)
    assert any("/rest/companies" in u for u in _Twenty.posts)
    assert any("/rest/opportunities" in u for u in _Twenty.posts)


def test_invoke_send_all_dispatches_approved(client):
    _result(client, "outreach.refresh", {})
    acct = core.DEMO[0]["account"]
    _result(client, "outreach.approve", {"account": acct})
    res = _result(client, "outreach.send_all", {})
    assert res["ok"] is True and res["action"] == "send_all"
    assert res["sent"] == [acct]               # only the approved sequence goes out


def test_idempotency_dedupes_side_effect(client):
    _result(client, "outreach.refresh", {})
    acct = core.DEMO[0]["account"]
    _Twenty.posts = []
    first = _result(client, "outreach.approve", {"account": acct}, idempotency_key="k-1")
    second = _result(client, "outreach.approve", {"account": acct}, idempotency_key="k-1")
    assert first == second
    # exactly-once: the pilot is synced to Twenty only on the first invoke
    assert [u for u in _Twenty.posts if "/rest/companies" in u] == ["http://twenty:3000/rest/companies"]


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract over a transport."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"outreach-engine": "http://outreach"}, transport=_transport)
    refreshed = oc.invoke("outreach-engine", "outreach.refresh", {}, idempotency_key="m-1")
    assert refreshed["n"] == len(core.DEMO)

    acct = core.DEMO[0]["account"]
    approved = oc.invoke("outreach-engine", "outreach.approve", {"account": acct}, idempotency_key="m-2")
    assert approved["twenty_synced"] is True and approved["account"] == acct
