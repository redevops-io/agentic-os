"""Route-level approval gating for agentic-crm.

Classification (documented in operator.py): `qualify` changes a Lead's status (a mutation of the customer record
beyond a draft) => APPROVAL-GATED inside core. score_lead (agent-owned lead_score + Comment), research and
draft_outreach (Comment only, a human sends) and ask (read-only) are annotations/drafts and stay ungated; nothing
in this app sends to a prospect.

Real FastAPI app via TestClient over a fake ERPNext transport; every action in the dispatch is exercised.
"""
from __future__ import annotations

import ast
import base64
import importlib
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

app_mod = importlib.import_module("agentic-crm.app")
core = importlib.import_module("agentic-crm.core")

CRED = "operator:s3cret-operator-pass"
LEAD = {"name": "CRM-LEAD-0001", "lead_name": "Dana Rivera", "company_name": "Whitfield", "status": "Open",
        "source": "Website", "creation": "2026-06-01 09:00:00"}
BODIES = {
    "score_lead": {"action": "score_lead", "lead": "CRM-LEAD-0001", "method": "rules"},
    "research": {"action": "research", "lead": "CRM-LEAD-0001"},
    "draft_outreach": {"action": "draft_outreach", "lead": "CRM-LEAD-0001"},
    "qualify": {"action": "qualify", "lead": "CRM-LEAD-0001", "status": "Replied"},
    "ask": {"action": "ask", "q": "how many leads?"},
}
GATED = ("qualify",)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class _Erp:
    puts: list[str] = []
    posts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        if "/api/resource/Lead/" in url:
            return _Resp(200, {"data": LEAD})
        return _Resp(200, {"data": []})

    def post(self, url, headers=None, json=None):
        _Erp.posts.append(url)
        return _Resp(200, {})

    def put(self, url, headers=None, json=None):
        _Erp.puts.append(json)
        return _Resp(200, {})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _Erp.puts, _Erp.posts = [], []
    core._CACHE.update(ts=0.0, data=None)
    monkeypatch.setattr(core, "httpx", types.SimpleNamespace(get=lambda *a, **k: _Resp(200),
                                                             Client=lambda timeout=None: _Erp()))
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", CRED)
    monkeypatch.delenv("OUTREACH_BASIC_AUTH", raising=False)
    monkeypatch.setattr(app_mod, "_llm_text", lambda *a, **k: "stub model text")
    yield


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


def _basic(cred=CRED):
    return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


def _staged(resp):
    j = resp.json()
    return resp.status_code == 200 and j.get("status") == "pending_approval" and j.get("executed") is False


def test_dispatch_covers_every_action():
    assert set(BODIES) == set(app_mod._ACTIONS)


def test_unauthenticated_qualify_is_staged_and_writes_nothing(client):
    assert _staged(client.post("/agent/run", json=BODIES["qualify"]))
    assert _Erp.puts == [] and _Erp.posts == []


@pytest.mark.parametrize("hdr", [
    _basic("operator:wrong"), _basic("wrong:s3cret-operator-pass"), _basic(":"), _basic("operator"),
    {"Authorization": "Basic ***"}, {"Authorization": "Bearer " + CRED}, {"Authorization": "Basic"}])
def test_wrong_credentials_are_staged(client, hdr):
    assert _staged(client.post("/agent/run", json=BODIES["qualify"], headers=hdr))
    assert _Erp.puts == [] and _Erp.posts == []


def test_operator_credential_unset_fails_closed(client, monkeypatch):
    monkeypatch.delenv("OPERATOR_BASIC_AUTH", raising=False)
    for cred in (CRED, ":", "operator:"):
        assert _staged(client.post("/agent/run", json=BODIES["qualify"], headers=_basic(cred)))
    assert _Erp.puts == []


@pytest.mark.parametrize("extra", [{"_approval": True}, {"_approval": {"approved": True}}, {"_Approval": 1},
                                   {"__approval": 1}, {"_APPROVAL_token": "t"}])
def test_forged_approval_in_body_is_staged(client, extra):
    assert _staged(client.post("/agent/run", json={**BODIES["qualify"], **extra}))
    assert _staged(client.post("/agent/run", json={**BODIES["qualify"], **extra}, headers=_basic("operator:bad")))
    assert _Erp.puts == [] and _Erp.posts == []


def test_operator_credential_executes_qualify_once(client):
    r = client.post("/agent/run", json=BODIES["qualify"], headers=_basic())
    assert r.json()["status"] == "done" and r.json()["new_status"] == "Replied"
    assert _Erp.puts == [{"status": "Replied"}] and len(_Erp.posts) == 1


@pytest.mark.parametrize("action", ["score_lead", "research", "draft_outreach", "ask"])
def test_ungated_annotation_actions_run_without_credentials_and_never_change_status(client, action):
    r = client.post("/agent/run", json=BODIES[action])
    assert r.status_code == 200 and r.json()["status"] == "done", r.json()
    assert all("status" not in (p or {}) for p in _Erp.puts)        # no Lead status change possible
    assert all(u.endswith("/api/resource/Comment") for u in _Erp.posts)


def test_unknown_action_400(client):
    assert client.post("/agent/run", json={"action": "x"}, headers=_basic()).status_code == 400


def test_assistant_never_receives_the_operator_marker(client):
    for msg in ("qualify CRM-LEAD-0001 as Replied", "advance the lead", "lead queue"):
        r = client.post("/api/agent", json={"message": msg, "action": "qualify", "lead": "CRM-LEAD-0001",
                                            "_approval": {"approved": True}}, headers=_basic())
        assert r.status_code == 200
    assert _Erp.puts == [] and _Erp.posts == []


def test_forged_body_marker_is_replaced_by_the_server_side_marker(client, monkeypatch):
    seen = []
    orig = core.qualify
    monkeypatch.setattr(core, "qualify", lambda b: (seen.append(dict(b)), orig(b))[1])
    client.post("/agent/run", json={**BODIES["qualify"], "_approval": "forged"}, headers=_basic())
    client.post("/agent/run", json={**BODIES["qualify"], "_approval": "forged"})
    assert seen[0]["_approval"] == {"approved": True, "by": "operator-basic"} and "_approval" not in seen[1]


# ── AST ──────────────────────────────────────────────────────────────────────────────────────────────────
WRAPPERS = {"_score_lead", "_research", "_draft_outreach", "_qualify", "_ask", "_ACTIONS"}
CORE_ACTIONS = {"score_lead", "research", "draft_outreach", "qualify", "ask"}


def test_routes_reach_core_actions_only_through_run_action():
    tree = ast.parse(Path(app_mod.__file__).read_text())
    routes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                      and d.func.attr in {"get", "post"} for d in n.decorator_list)]
    assert {r.name for r in routes} >= {"agent_run", "api_agent"}
    for fn in routes:
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "core":
                assert n.attr not in CORE_ACTIONS, f"{fn.name} calls core.{n.attr} directly"
            if isinstance(n, ast.Name):
                assert n.id not in WRAPPERS, f"{fn.name} references {n.id}"
    ra = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_action")
    assert any(isinstance(n, ast.Attribute) and n.attr == "with_operator_approval" for n in ast.walk(ra))
    # the assistant chain (_agent_reply and the read-only tools) never touches the action wrappers / core actions
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    seen, stack = set(), ["_agent_reply"]
    while stack:
        nm = stack.pop()
        if nm in seen or nm not in fns:
            continue
        seen.add(nm)
        for n in ast.walk(fns[nm]):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "core":
                assert n.attr not in CORE_ACTIONS
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                stack.append(n.id)
    assert not (WRAPPERS | {"_run_action"}) & seen
