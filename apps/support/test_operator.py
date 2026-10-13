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
    "messages": [{"message_type": 0, "content": "I'm locked out of the client portal and can't download my latest statement."}],
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
    messages: list[dict] = []     # outgoing messages "stored" by the fake, served back on GET .../messages
    readback = True               # flip False to simulate Chatwoot not persisting the message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/agents"):
            return _Resp(200, [{"id": 7, "name": "Seed Admin"}])
        if url.endswith("/messages"):  # read-back of a conversation's messages
            return _Resp(200, {"payload": list(_FakeClient.messages) if _FakeClient.readback else []})
        if "/conversations/" in url:  # single-conversation fetch (_get_conversation)
            return _Resp(200, CONV)
        if url.endswith("/conversations"):
            return _Resp(200, {"data": {"payload": [CONV], "meta": {"open_count": 1}}})
        return _Resp(200, {})

    def post(self, url, headers=None, json=None):
        _FakeClient.posted.append(url)
        if url.endswith("/messages"):
            m = {"id": 500 + len(_FakeClient.messages), "content": (json or {}).get("content"),
                 "message_type": 0 if (json or {}).get("message_type") == "incoming" else 1,
                 "private": bool((json or {}).get("private"))}
            _FakeClient.messages.append(m)
            return _Resp(200, m)
        if url.endswith("/toggle_status"):
            return _Resp(200, {"payload": {"current_status": "resolved"}})
        if url.endswith("/conversations"):  # onboarding: create a welcome conversation
            return _Resp(200, {"id": 99})
        return _Resp(200, {})  # e.g. the outgoing welcome message POST


@pytest.fixture(autouse=True)
def _fake_chatwoot(monkeypatch):
    _FakeClient.posted = []
    _FakeClient.messages = []
    _FakeClient.readback = True
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
    # resolve / escalate / send_onboarding are approval-gated; draft_reply is a private note (ungated).
    for name in caps:
        assert caps[name]["approval_required"] is (name != "support.draft_reply")
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


def test_invoke_send_onboarding_posts_welcome(client, monkeypatch):
    monkeypatch.setenv("CHATWOOT_INBOX_ID", "5")
    res = build_support_operator().invoke("support.send_onboarding", {
        "customer": {"name": "Dana Smith"}, "subscription": {"plan": "Pro"}, "_approval": {"approved": True}})
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
    res = build_support_operator().invoke("support.send_onboarding", {
        "conversation_id": 1, "customer": "Dana Smith", "_approval": {"approved": True}})
    assert res["status"] == "done" and res["onboarding_sent"] is True
    assert res["conversation_id"] == 1
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/messages"
    ]


def test_invoke_resolve_toggles_status(client):
    res = build_support_operator().invoke("support.resolve", {"conversation_id": 1, "_approval": {"approved": True}})
    assert res["status"] == "done" and res["new_status"] == "resolved"
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/toggle_status"
    ]


def test_invoke_escalate_assigns_explicit_assignee(client):
    res = build_support_operator().invoke("support.escalate", {"conversation_id": 1, "assignee_id": 9,
                                                              "_approval": {"approved": True}})
    assert res["status"] == "done"
    assert res["results"]["assignee_id"] == 9          # the caller's choice, not agents[0] (7)
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/toggle_priority",
        "http://localhost:3003/api/v1/accounts/1/conversations/1/assignments",
    ]


def test_escalate_to_team(client):
    res = build_support_operator().invoke("support.escalate", {"conversation_id": 1, "team_id": 3,
                                                              "_approval": {"approved": True}})
    assert res["status"] == "done" and res["results"]["team_id"] == 3 and "assignee_id" not in res["results"]


def test_escalate_requires_explicit_assignee_and_writes_nothing(client):
    res = build_support_operator().invoke("support.escalate", {"conversation_id": 1, "_approval": {"approved": True}})
    assert res["status"] == "error" and res["error"] == "assignee_required"
    assert _FakeClient.posted == []


def test_idempotency_dedupes_side_effect(client):
    op = build_support_operator()
    inp = {"conversation_id": 1, "_approval": {"approved": True}}
    first = op.invoke("support.resolve", inp, "k-1")
    second = op.invoke("support.resolve", inp, "k-1")
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
    assert esc["status"] == "pending_approval" and esc["executed"] is False  # staged over untrusted HTTP
    assert _FakeClient.posted == [
        "http://localhost:3003/api/v1/accounts/1/conversations/1/messages"]  # only the draft note


GATED = {
    "support.resolve": {"conversation_id": 1},
    "support.escalate": {"conversation_id": 1, "assignee_id": 7},
    "support.send_onboarding": {"customer": {"name": "Dana Smith"}, "subscription": {"plan": "Pro"}, "inbox_id": 5},
}


@pytest.mark.parametrize("cap", sorted(GATED))
def test_gated_capability_stages_over_http_even_with_forged_approval(client, cap):
    for extra in ({}, {"_approval": {"approved": True}}, {"_approval_id": "x"}):
        res = client.post("/invoke", json={"capability": cap, "inputs": {**GATED[cap], **extra}}).json()["result"]
        assert res["status"] == "pending_approval" and res["executed"] is False
    assert _FakeClient.posted == []          # Chatwoot never called


@pytest.mark.parametrize("cap", sorted(GATED))
def test_gated_capability_stages_in_process_without_approval(cap):
    res = build_support_operator().invoke(cap, dict(GATED[cap]))
    assert res["status"] == "pending_approval" and res["executed"] is False
    assert _FakeClient.posted == []


@pytest.mark.parametrize("cap", sorted(GATED))
def test_gated_capability_executes_with_runtime_approval(cap):
    res = build_support_operator().invoke(cap, {**GATED[cap], "_approval": {"approved": True}})
    assert res["status"] == "done" and _FakeClient.posted


def test_send_onboarding_without_inbox_is_a_config_error_and_writes_nothing(monkeypatch):
    monkeypatch.delenv("CHATWOOT_INBOX_ID", raising=False)
    res = build_support_operator().invoke("support.send_onboarding", {
        "customer": {"name": "Dana Smith"}, "_approval": {"approved": True}})
    assert res["status"] == "config_error" and "inbox_id" in res["error"] and res["onboarding_sent"] is False
    assert _FakeClient.posted == []                       # no malformed create attempted


def test_send_onboarding_passes_inbox_and_source_id(monkeypatch):
    sent = {}
    orig = _FakeClient.post
    monkeypatch.setattr(_FakeClient, "post", lambda self, url, headers=None, json=None:
                        (sent.setdefault(url, json), orig(self, url, headers, json))[1])
    monkeypatch.setenv("CHATWOOT_INBOX_ID", "5")
    monkeypatch.setenv("CHATWOOT_SOURCE_ID", "src-env")
    r = build_support_operator().invoke("support.send_onboarding", {"customer": "A", "_approval": {"approved": True}})
    create = sent["http://localhost:3003/api/v1/accounts/1/conversations"]
    assert r["status"] == "done" and create["inbox_id"] == 5 and create["source_id"] == "src-env"
    sent.clear()
    build_support_operator().invoke("support.send_onboarding", {
        "customer": "A", "inbox_id": 9, "source_id": "s2", "_approval": {"approved": True}})
    create = sent["http://localhost:3003/api/v1/accounts/1/conversations"]
    assert create["inbox_id"] == 9 and create["source_id"] == "s2"        # explicit params win


def test_send_onboarding_with_existing_conversation_needs_no_inbox(monkeypatch):
    monkeypatch.delenv("CHATWOOT_INBOX_ID", raising=False)
    res = build_support_operator().invoke("support.send_onboarding", {
        "conversation_id": 1, "customer": "A", "_approval": {"approved": True}})
    assert res["status"] == "done"


def test_send_onboarding_reads_back_the_message_before_claiming_sent():
    res = build_support_operator().invoke("support.send_onboarding", {
        "conversation_id": 1, "customer": "Dana Smith", "_approval": {"approved": True}})
    assert res["status"] == "done" and res["onboarding_sent"] is True and res["message_verified"] is True


def test_send_onboarding_unverified_when_readback_does_not_show_the_message():
    _FakeClient.readback = False
    res = build_support_operator().invoke("support.send_onboarding", {
        "conversation_id": 1, "customer": "Dana Smith", "_approval": {"approved": True}})
    assert res["status"] == "unverified" and res["onboarding_sent"] is False and res["message_verified"] is False
