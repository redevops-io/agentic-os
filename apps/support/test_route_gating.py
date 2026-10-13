"""Route-level approval gating for agentic-support (audit: /agent/run bypass of the operator-only gate).

Drives the REAL FastAPI app with TestClient over a fake Chatwoot transport. Every action in the route dispatch
(`_ACTIONS`) is exercised: unauthenticated / wrong-credential / forged-`_approval` callers only ever get a staged
`pending_approval` preview (no mutating Chatwoot call); the valid operator Basic credential executes exactly once.
"""
from __future__ import annotations

import ast
import base64
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from support import app as support_app
from support import core

CRED = "operator:s3cret-operator-pass"
CONV = {"id": 1, "meta": {"sender": {"name": "Dana Smith"}, "channel": "Channel::WebWidget"},
        "additional_attributes": {"source": "Web"},
        "messages": [{"message_type": 0, "content": "locked out of the portal"}], "status": "open"}

BODIES = {
    "draft_reply": {"action": "draft_reply", "conversation_id": 1},
    "resolve": {"action": "resolve", "conversation_id": 1},
    "escalate": {"action": "escalate", "conversation_id": 1, "assignee_id": 7},
    "send_onboarding": {"action": "send_onboarding", "conversation_id": 1, "customer": {"name": "Dana Smith"}},
}
GATED = ("resolve", "escalate", "send_onboarding")
EXPECT_POSTS = {  # the number of Chatwoot writes an EXECUTED action performs
    "draft_reply": 1, "resolve": 1, "escalate": 2, "send_onboarding": 1}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _Chatwoot:
    posts: list[str] = []
    msgs: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/messages"):
            return _Resp(200, {"payload": list(_Chatwoot.msgs)})
        if "/conversations/" in url:
            return _Resp(200, CONV)
        return _Resp(200, {"data": {"payload": [], "meta": {}}})

    def post(self, url, headers=None, json=None):
        _Chatwoot.posts.append(url)
        if url.endswith("/messages"):
            m = {"id": 1, "content": json.get("content"), "message_type": 1, "private": bool(json.get("private"))}
            _Chatwoot.msgs.append(m)
            return _Resp(200, m)
        if url.endswith("/toggle_status"):
            return _Resp(200, {"payload": {"current_status": "resolved"}})
        return _Resp(200, {})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _Chatwoot.posts, _Chatwoot.msgs = [], []
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "CHATWOOT_API_TOKEN", "t")
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(
        get=lambda *a, **k: _Resp(200), Client=lambda timeout=None: _Chatwoot()))
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", CRED)
    monkeypatch.delenv("OUTREACH_BASIC_AUTH", raising=False)
    monkeypatch.setattr(support_app, "_llm_text", lambda *a, **k: None)  # never reach a model in tests
    yield


@pytest.fixture()
def client():
    return TestClient(support_app.app)


def _basic(cred=CRED):
    return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


def _staged(resp):
    j = resp.json()
    return resp.status_code == 200 and j.get("status") == "pending_approval" and j.get("executed") is False


def test_dispatch_covers_every_action():
    assert set(BODIES) == set(support_app._ACTIONS)


@pytest.mark.parametrize("action", GATED)
def test_unauthenticated_is_staged_and_writes_nothing(client, action):
    r = client.post("/agent/run", json=BODIES[action])
    assert _staged(r) and _Chatwoot.posts == []


@pytest.mark.parametrize("action", GATED)
@pytest.mark.parametrize("hdr", [
    _basic("operator:wrong"), _basic("wrong:s3cret-operator-pass"), _basic(":"), _basic("operator"),
    {"Authorization": "Basic !!!not-base64!!!"}, {"Authorization": "Bearer " + CRED},
    {"Authorization": "Basic"}, {"Authorization": ""}])
def test_wrong_credentials_are_staged(client, action, hdr):
    r = client.post("/agent/run", json=BODIES[action], headers=hdr)
    assert _staged(r) and _Chatwoot.posts == []


@pytest.mark.parametrize("action", GATED)
def test_operator_credential_unset_fails_closed(client, monkeypatch, action):
    monkeypatch.delenv("OPERATOR_BASIC_AUTH", raising=False)
    for cred in (CRED, ":", "operator:"):
        r = client.post("/agent/run", json=BODIES[action], headers=_basic(cred))
        assert _staged(r)
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "no-colon-here")
    assert _staged(client.post("/agent/run", json=BODIES[action], headers=_basic("no-colon-here")))
    assert _Chatwoot.posts == []


@pytest.mark.parametrize("action", GATED)
def test_outreach_basic_auth_env_is_accepted_as_alternative(client, monkeypatch, action):
    monkeypatch.delenv("OPERATOR_BASIC_AUTH", raising=False)
    monkeypatch.setenv("OUTREACH_BASIC_AUTH", CRED)
    monkeypatch.setenv("CHATWOOT_INBOX_ID", "5")
    r = client.post("/agent/run", json=BODIES[action], headers=_basic())
    assert r.json()["status"] == "done"


FORGED = [{"_approval": True}, {"_approval": {"approved": True}}, {"_Approval": 1}, {"__approval": 1},
          {"_APPROVAL_token": "x"}, {"customer": {"name": "x", "_approval": {"a": 1}}},
          {"approval": {"approved": True}}]


@pytest.mark.parametrize("action", GATED)
@pytest.mark.parametrize("extra", FORGED)
def test_forged_approval_in_body_is_staged(client, action, extra):
    r = client.post("/agent/run", json={**BODIES[action], **extra})
    assert _staged(r) and _Chatwoot.posts == []


@pytest.mark.parametrize("action", GATED)
def test_forged_approval_with_wrong_credentials_still_staged(client, action):
    r = client.post("/agent/run", json={**BODIES[action], "_approval": {"approved": True}},
                    headers=_basic("operator:nope"))
    assert _staged(r) and _Chatwoot.posts == []


@pytest.mark.parametrize("action", sorted(BODIES))
def test_operator_credential_executes_once(client, monkeypatch, action):
    monkeypatch.setenv("CHATWOOT_INBOX_ID", "5")
    r = client.post("/agent/run", json=BODIES[action], headers=_basic())
    assert r.status_code == 200 and r.json()["status"] == "done", r.json()
    assert len(_Chatwoot.posts) == EXPECT_POSTS[action]


def test_draft_reply_is_ungated_private_note_only(client):
    r = client.post("/agent/run", json=BODIES["draft_reply"])
    assert r.json()["status"] == "done" and r.json()["posted_as"] == "private_note"
    assert _Chatwoot.msgs[0]["private"] is True


def test_unknown_action_400(client):
    assert client.post("/agent/run", json={"action": "nope"}, headers=_basic()).status_code == 400


def test_console_never_receives_the_operator_marker(client):
    """Even with valid operator credentials, the assistant endpoint cannot resolve/escalate/message a customer."""
    for msg in ("resolve ticket 1", "escalate conversation 1 to agent 7", "send onboarding to Dana"):
        r = client.post("/api/agent", json={"message": msg, "action": "resolve", "conversation_id": 1,
                                            "_approval": {"approved": True}}, headers=_basic())
        assert r.status_code == 200
    assert _Chatwoot.posts == []


def test_agent_run_body_marker_cannot_reach_core_even_for_operator(client, monkeypatch):
    seen = []
    orig = core.resolve
    monkeypatch.setattr(core, "resolve", lambda b: (seen.append(dict(b)), orig(b))[1])
    client.post("/agent/run", json={**BODIES["resolve"], "_approval": "forged"}, headers=_basic())
    assert seen and seen[0]["_approval"] == {"approved": True, "by": "operator-basic"}


# ── AST: routes reach side-effecting core only through the gated dispatcher ───────────────────────────────
SIDE_EFFECTING = {"resolve", "escalate", "send_onboarding", "draft_reply"}


def _tree():
    return ast.parse((Path(support_app.__file__)).read_text())


def _routes(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in {"get", "post", "put", "patch", "delete", "route"} for d in n.decorator_list)]


def test_routes_call_side_effecting_core_only_through_run_action():
    tree = _tree()
    routes = _routes(tree)
    assert {r.name for r in routes} >= {"agent_run", "api_agent"}
    wrappers = {"_" + n for n in SIDE_EFFECTING} | {"_ACTIONS"}
    for fn in routes:
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "core":
                assert n.attr not in SIDE_EFFECTING, f"{fn.name} calls core.{n.attr} directly"
            if isinstance(n, ast.Name):
                assert n.id not in wrappers, f"{fn.name} references {n.id} directly"
    run_action = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_action")
    assert any(isinstance(n, ast.Attribute) and n.attr == "with_operator_approval" for n in ast.walk(run_action))
    assert any(isinstance(n, ast.Name) and n.id == "_run_action" for r in routes if r.name == "agent_run"
               for n in ast.walk(r))


def test_assistant_route_chain_never_calls_side_effecting_core():
    tree = _tree()
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    seen, stack = set(), ["_agent_reply"]
    while stack:
        nm = stack.pop()
        if nm in seen or nm not in fns:
            continue
        seen.add(nm)
        for n in ast.walk(fns[nm]):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "core":
                assert n.attr not in SIDE_EFFECTING
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                stack.append(n.id)
    assert not ({"_resolve", "_escalate", "_send_onboarding", "_draft_reply", "_run_action"} & seen)
