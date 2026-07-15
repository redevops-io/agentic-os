"""agentic-support as a Mission Runtime operator — proof over a fake Chatwoot core.

Exercises the REAL core logic (draft_reply private note, resolve toggle_status, escalate
priority+assign) and the SDK-mount contract (GET /capabilities + POST /invoke) end-to-end,
plus the Mission Runtime's own HTTPOperatorClient driving the operator over the wire with
exactly-once idempotency.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/support/test_operator.py -q
"""
from __future__ import annotations

import types
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from support import core
from support.operator import build_support_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake Chatwoot core (one open conversation) ─────────────────────────────
CONV = {
    "id": 1,
    "meta": {"sender": {"name": "Dana Smith"}, "channel": "Channel::WebWidget"},
    "additional_attributes": {"source": "Web"},
    "messages": [{"message_type": 0, "content": "My roof is leaking after the storm."}],
    "status": "open",
    "priority": None,
    "created_at": 1700000000,
}


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
    return _Resp(200)  # connectivity check


class _FakeClient:
    """Stands in for httpx.Client — records the write POSTs to Chatwoot."""
    posted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/agents"):
            return _Resp(200, [{"id": 7, "name": "Seed Admin"}])
        if "/conversations/" in url:  # single-conversation fetch (_get_conversation)
            return _Resp(200, CONV)
        if url.endswith("/conversations"):
            return _Resp(200, {"data": {"payload": [CONV], "meta": {"open_count": 1}}})
        return _Resp(200, {})

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append(url)
        if url.endswith("/toggle_status"):
            return _Resp(200, {"payload": {"current_status": "resolved"}})
        if url.endswith("/conversations"):  # onboarding: create a welcome conversation
            return _Resp(200, {"id": 99})
        return _Resp(200, {})  # e.g. the outgoing welcome message POST


@pytest.fixture(autouse=True)
def _fake_chatwoot(monkeypatch):
    _FakeClient.posted = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core, "CHATWOOT_API_TOKEN", "test-key")
    monkeypatch.setattr(core, "httpx",
                        types.SimpleNamespace(get=_fake_get, Client=lambda timeout=None: _FakeClient()))
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_support_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"support.draft_reply", "support.resolve", "support.escalate",
                         "support.send_onboarding"}
    # agentic-support has NO approval gates (modules.yaml approval_required: []).
    for name in caps:
        assert caps[name]["approval_required"] is False
        assert caps[name]["side_effecting"] is True  # each writes to Chatwoot
    assert "reply_drafted" in caps["support.draft_reply"]["provides"]
    assert "ticket_resolved" in caps["support.resolve"]["provides"]
    assert "ticket_escalated" in caps["support.escalate"]["provides"]
    assert "onboarding_sent" in caps["support.send_onboarding"]["provides"]


def test_invoke_draft_reply_posts_private_note(client):
    r = client.post("/invoke", json={"capability": "support.draft_reply",
                                      "inputs": {"conversation_id": 1}}).json()
    res = r["result"]
    assert res["status"] == "done"
    assert res["posted_as"] == "private_note"
    assert res["draft_source"] == "deterministic template"  # operator passes no blurb
    # the REAL core posted the draft as a private note against the (fake) Chatwoot
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/messages"
    ]


def test_invoke_send_onboarding_posts_welcome(client):
    r = client.post("/invoke", json={
        "capability": "support.send_onboarding",
        "inputs": {"customer": {"name": "Dana Smith"},
                   "subscription": {"plan": "Pro"}},
    }).json()
    res = r["result"]
    assert res["status"] == "done"
    assert res["onboarding_sent"] is True
    assert res["conversation_id"] == 99  # id returned by the fake conversation-create
    # the REAL core created a welcome conversation then posted the outgoing welcome message
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations",
        "http://localhost:3003/api/v1/accounts/1/conversations/99/messages",
    ]


def test_invoke_send_onboarding_reuses_conversation(client):
    """A supplied conversation_id skips the create call and posts straight to it."""
    res = client.post("/invoke", json={
        "capability": "support.send_onboarding",
        "inputs": {"conversation_id": 1, "customer": "Dana Smith"},
    }).json()["result"]
    assert res["status"] == "done" and res["onboarding_sent"] is True
    assert res["conversation_id"] == 1
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/messages"
    ]


def test_invoke_resolve_toggles_status(client):
    res = client.post("/invoke", json={"capability": "support.resolve",
                                        "inputs": {"conversation_id": 1}}).json()["result"]
    assert res["status"] == "done" and res["new_status"] == "resolved"
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/toggle_status"
    ]


def test_invoke_escalate_sets_priority_and_assigns(client):
    res = client.post("/invoke", json={"capability": "support.escalate",
                                        "inputs": {"conversation_id": 1}}).json()["result"]
    assert res["status"] == "done"
    assert res["results"]["assignee_id"] == 7
    # priority toggled + assignment posted (agents fetched in between via GET)
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/toggle_priority",
        "http://localhost:3003/api/v1/accounts/1/conversations/1/assignments",
    ]


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "support.resolve", "inputs": {"conversation_id": 1},
            "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second
    # exactly-once: the toggle_status write happened a single time
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/toggle_status"
    ]


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"support": "http://support"}, transport=_transport)
    dr = oc.invoke("support", "support.draft_reply", {"conversation_id": 1}, idempotency_key="m-1")
    assert dr["status"] == "done" and dr["posted_as"] == "private_note"

    esc = oc.invoke("support", "support.escalate", {"conversation_id": 1}, idempotency_key="m-2")
    assert esc["status"] == "done" and esc["results"]["assignee_id"] == 7
