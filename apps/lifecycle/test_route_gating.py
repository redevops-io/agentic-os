"""Route-level classification test for lifecycle.

Audit result: NOTHING in this app sends to a contact or changes a customer record beyond a draft --
compose_campaign creates a Listmonk campaign DRAFT (read back and verified as `draft`), segment / suggest_flow are
advisory. No action starts, schedules or changes the status of a campaign, so none is approval-gated. These tests pin
that classification at the route: whatever the caller sends (credentials, forged `_approval`, `status`/`send` keys in
the body, the assistant endpoint), the only Listmonk call besides reads is the single draft POST /api/campaigns.
"""
from __future__ import annotations

import ast
import base64
import json
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lifecycle import app as lc_app
from lifecycle import core

BODIES = {
    "compose_campaign": {"action": "compose_campaign", "prompt": "welcome series", "list_id": 3},
    "segment": {"action": "segment", "goal": "inactive 90 days"},
    "suggest_flow": {"action": "suggest_flow", "trigger": "welcome"},
}
CRED = "operator:s3cret-operator-pass"


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload or {}, ""

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _Listmonk:
    calls: list[tuple[str, str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        _Listmonk.calls.append(("GET", url, {}))
        if "/api/campaigns/" in url:
            return _Resp(200, {"data": {"id": 11, "status": "draft"}})
        return _Resp(200, {"data": {}})

    def post(self, url, headers=None, json=None):
        _Listmonk.calls.append(("POST", url, json or {}))
        return _Resp(200, {"data": {"id": 11, "status": "draft"}})

    def put(self, url, headers=None, json=None):
        _Listmonk.calls.append(("PUT", url, json or {}))
        return _Resp(200, {"data": {}})

    delete = patch = put


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _Listmonk.calls = []
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=lambda *a, **k: _Resp(200),
                                                             Client=lambda timeout=None: _Listmonk()))
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", CRED)
    monkeypatch.setattr(lc_app, "_llm_text", lambda p, n=600: json.dumps(
        {"name": "n", "subject": "s", "body_html": "<p>x</p>", "query": "q", "rationale": "r", "flow": "f", "steps": []}))
    yield


@pytest.fixture()
def client():
    return TestClient(lc_app.app)


def _writes():
    return [(m, u) for m, u, _ in _Listmonk.calls if m != "GET"]


def test_dispatch_covers_every_action():
    assert set(BODIES) == set(lc_app._ACTIONS)


@pytest.mark.parametrize("action", sorted(BODIES))
@pytest.mark.parametrize("extra,hdr", [
    ({}, {}),
    ({"_approval": {"approved": True}, "status": "running", "send": True, "start": True},
     {"Authorization": "Basic " + base64.b64encode(CRED.encode()).decode()}),
    ({"_Approval": 1, "__approval": 1}, {"Authorization": "Basic bad"})])
def test_no_action_can_send_or_change_campaign_status(client, action, extra, hdr):
    r = client.post("/agent/run", json={**BODIES[action], **extra}, headers=hdr)
    assert r.status_code == 200
    assert "pending_approval" != r.json().get("status")        # nothing here is a gated send
    w = _writes()
    assert w in ([], [("POST", core.LISTMONK_API_URL + "/api/campaigns")]), w
    if action == "compose_campaign":
        assert r.json()["campaign_drafted"] is True
        post = next(b for m, u, b in _Listmonk.calls if m == "POST")
        assert post.get("type") == "regular" and "status" not in post and "send_at" not in post


def test_assistant_endpoint_has_no_write_path(client):
    for msg in ("send the campaign now", "compose a campaign", "campaign status", "list segments"):
        assert client.post("/api/agent", json={"message": msg, "action": "compose_campaign",
                                               "_approval": {"approved": True}}).status_code == 200
    assert _writes() == []


def test_unknown_action_400(client):
    assert client.post("/agent/run", json={"action": "send"}).status_code == 400


def test_app_and_core_never_reference_a_listmonk_send_or_status_endpoint():
    for f in (Path(lc_app.__file__), Path(core.__file__)):
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                v = n.value
                assert "/status" not in v and "/test" not in v and "/api/tx" not in v, (f.name, v[:60])
        calls = {a.attr for a in ast.walk(tree) if isinstance(a, ast.Attribute)}
        assert not ({"put", "patch", "delete"} & calls), f"{f.name} issues a non-draft write verb"
