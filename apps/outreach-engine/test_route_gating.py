"""Route-level approval gating for outreach-engine (audit: /agent/run + /api/run + /api/discover bypass).

Real FastAPI app via TestClient over a fake Twenty transport (urllib.request.urlopen). Every action in the route
dispatch (`_ACTIONS`) is covered: unauthenticated / wrong-credential / forged-`_approval` callers only get a
staged `pending_approval` (no Twenty write, account not approved); the valid operator Basic credential executes once.
"""
from __future__ import annotations

import ast
import base64
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

app_mod = importlib.import_module("outreach-engine.app")
core = importlib.import_module("outreach-engine.core")

CRED = "operator:s3cret-operator-pass"
ACCT = core.DEMO[0]["account"]
BODIES = {
    "refresh": {"action": "refresh", "live": False},
    "enrich": {"action": "enrich", "account": ACCT},
    "draft_pitch": {"action": "draft_pitch", "account": ACCT},
    "approve": {"action": "approve", "account": ACCT},
    "send_all": {"action": "send_all"},
    "check_replies": {"action": "check_replies"},
}
GATED = ("approve", "send_all")
PATHS = ("/api/run", "/agent/run")


class _Twenty:
    writes: list[str] = []


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(req, timeout=None):
    url = getattr(req, "full_url", None) or str(req)
    method = req.get_method() if hasattr(req, "get_method") else "GET"
    if "/rest/" in url and method == "POST":
        _Twenty.writes.append(url)
        key = "createCompany" if "companies" in url else "createPerson" if "people" in url else "createOpportunity"
        return _Resp({"data": {key: {"id": "x-1"}}})
    if "api.github.com" in url:
        return _Resp({"items": []})
    if "hn.algolia.com" in url:
        return _Resp({"hits": []})
    return _Resp({})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    _Twenty.writes = []
    core._STATE.update(accounts=[], approved=set(), candidates=[])
    core._LIVE_CACHE.update(ts=0.0, rows=[])
    monkeypatch.setattr(core, "TWENTY_API_KEY", "k")
    monkeypatch.setattr(core.urllib.request, "urlopen", _urlopen)
    # hermetic against this machine's real ~/.zshrc exports: no test may attempt a real SMTP connection
    monkeypatch.setattr(core, "GMAIL_SMTP_USER", "")
    monkeypatch.setattr(core, "GMAIL_SMTP_APP_PASSWORD", "")
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", CRED)
    monkeypatch.delenv("OUTREACH_BASIC_AUTH", raising=False)
    monkeypatch.setattr(app_mod, "_llm", lambda *a, **k: "")
    yield


@pytest.fixture()
def client():
    c = TestClient(app_mod.app)
    c.post("/agent/run", json=BODIES["refresh"])      # populate _STATE["accounts"] (ungated read)
    return c


def _basic(cred=CRED):
    return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


def _staged(resp):
    j = resp.json()
    return resp.status_code == 200 and j.get("status") == "pending_approval" and j.get("executed") is False


def _nothing_happened():
    return _Twenty.writes == [] and ACCT not in core._STATE["approved"]


def test_dispatch_covers_every_action():
    assert set(BODIES) == set(app_mod._ACTIONS)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("action", GATED)
def test_unauthenticated_is_staged(client, path, action):
    r = client.post(path, json=BODIES[action])
    assert _staged(r) and _nothing_happened()


@pytest.mark.parametrize("action", GATED)
@pytest.mark.parametrize("hdr", [
    _basic("operator:wrong"), _basic("wrong:s3cret-operator-pass"), _basic(":"), _basic("operator"),
    {"Authorization": "Basic %%%"}, {"Authorization": "Bearer " + CRED}, {"Authorization": "Basic"}])
def test_wrong_credentials_are_staged(client, action, hdr):
    assert _staged(client.post("/api/run", json=BODIES[action], headers=hdr)) and _nothing_happened()


@pytest.mark.parametrize("action", GATED)
def test_operator_credential_unset_fails_closed(client, monkeypatch, action):
    monkeypatch.delenv("OPERATOR_BASIC_AUTH", raising=False)
    for cred in (CRED, ":", "operator:", ":pw"):
        assert _staged(client.post("/api/run", json=BODIES[action], headers=_basic(cred)))
    assert _nothing_happened()


@pytest.mark.parametrize("extra", [{"_approval": True}, {"_approval": {"approved": True}}, {"_Approval": 1},
                                   {"__approval": 1}, {"_APPROVAL_token": "t"}])
@pytest.mark.parametrize("action", GATED)
def test_forged_approval_in_body_is_staged(client, action, extra):
    for path in PATHS:
        assert _staged(client.post(path, json={**BODIES[action], **extra})) and _nothing_happened()
        assert _staged(client.post(path, json={**BODIES[action], **extra}, headers=_basic("operator:bad")))
    assert _nothing_happened()


def test_operator_approve_executes_once(client):
    r = client.post("/api/run", json=BODIES["approve"], headers=_basic())
    assert r.json()["ok"] is True and r.json()["twenty_synced"] is True
    assert ACCT in core._STATE["approved"]
    assert sum("/rest/opportunities" in u for u in _Twenty.writes) == 1
    assert sum("/rest/companies" in u for u in _Twenty.writes) == 1


def test_operator_send_all_executes_through_core_honestly(client):
    core._STATE["approved"].add(ACCT)
    r = client.post("/agent/run", json=BODIES["send_all"], headers=_basic()).json()
    assert r["status"] == "not_configured" and r["sequences_sent"] is False and r["staged"] == [ACCT]


def test_refresh_is_ungated_read(client):
    r = client.post("/api/run", json=BODIES["refresh"])
    assert r.json()["action"] == "refresh" and _Twenty.writes == []


def test_draft_pitch_is_ungated_read(client):
    # app_mod._llm is forced to "" by the autouse fixture, so this exercises the deterministic
    # template fallback -- still runs with no approval marker, and still never touches Twenty.
    r = client.post("/api/run", json=BODIES["draft_pitch"])
    j = r.json()
    assert j["ok"] is True and j["action"] == "draft_pitch" and j["draft_source"] == "template"
    assert "<table" in j["html"] and _Twenty.writes == []
    row = next(row for row in core._STATE["accounts"] if row["account"] == ACCT)
    assert row["draft"]["body"] == j["body"]


def test_enrich_is_ungated_read(client):
    # DEMO leads are "manual" source (no GitHub profile) -> no domain -> honest degrade, but it never
    # needs an approval marker to run, and it never touches Twenty.
    r = client.post("/api/run", json=BODIES["enrich"])
    j = r.json()
    assert j["ok"] is True and j["action"] == "enrich" and j["email"] is None and _Twenty.writes == []


def test_check_replies_is_ungated_read_and_honest_without_a_reader(client):
    # the autouse fixture forces GMAIL_SMTP_USER/APP_PASSWORD off, so this is the honest "not configured"
    # path -- no approval marker needed either way, and it never touches Twenty.
    r = client.post("/api/run", json=BODIES["check_replies"])
    j = r.json()
    assert j["ok"] is False and j["action"] == "check_replies" and "no reader" in j["reason"]
    assert _Twenty.writes == []


def test_unknown_action(client):
    assert client.post("/api/run", json={"action": "nope"}, headers=_basic()).json()["ok"] is False


# ── /api/discover ────────────────────────────────────────────────────────────────────────────────────────
def _stub_discovery(monkeypatch):
    cands = [{"account": "Acme Health", "evidence": "RFP for records", "source": "usaspending", "amount": 20000}]
    monkeypatch.setattr(app_mod, "plan_search", lambda i: {"count": 3})
    monkeypatch.setattr(app_mod, "run_discovery", lambda plan: (cands, "test"))


def test_discover_unauthenticated_is_a_preview_and_persists_nothing(client, monkeypatch):
    _stub_discovery(monkeypatch)
    for kw in ({}, {"headers": _basic("operator:bad")}):
        r = client.post("/api/discover", json={"instruction": "find healthcare", "_approval": {"approved": True}}, **kw).json()
        assert r["persisted"] is False and r["status"] == "preview" and r["added"] == []
        assert r["candidates"][0]["account"] == "Acme Health"
    assert core._STATE["candidates"] == [] and _Twenty.writes == []


def test_discover_operator_persists_candidates_but_never_syncs_to_crm(client, monkeypatch):
    _stub_discovery(monkeypatch)
    r = client.post("/api/discover", json={"instruction": "find healthcare"}, headers=_basic()).json()
    assert r["persisted"] is True and r["added"] and r["crm_synced"] is False
    assert [c["account"] for c in core._STATE["candidates"]] == ["Acme Health"]
    assert _Twenty.writes == []


def test_assistant_never_receives_the_operator_marker(client, monkeypatch):
    _stub_discovery(monkeypatch)
    for msg in (f"approve {ACCT}", "send all approved sequences", "find healthcare leads"):
        r = client.post("/api/agent", json={"message": msg, "action": "approve", "account": ACCT,
                                            "_approval": {"approved": True}}, headers=_basic())
        assert r.status_code == 200
    assert _nothing_happened() and core._STATE["candidates"] == []


# ── AST ──────────────────────────────────────────────────────────────────────────────────────────────────
SIDE_EFFECTING = {"approve", "send_all", "twenty_sync"}


def _tree():
    return ast.parse(Path(app_mod.__file__).read_text())


def test_routes_reach_side_effecting_core_only_through_run_action():
    tree = _tree()
    routes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                      and d.func.attr in {"get", "post"} for d in n.decorator_list)]
    assert {r.name for r in routes} >= {"agent_run", "api_discover", "api_agent"}
    for fn in routes:
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "core":
                assert n.attr not in SIDE_EFFECTING, f"{fn.name} calls core.{n.attr} directly"
            if isinstance(n, ast.Name):
                assert n.id not in SIDE_EFFECTING | {"_ACTIONS"}, f"{fn.name} references {n.id}"
    ra = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_action")
    assert any(isinstance(n, ast.Attribute) and n.attr == "with_operator_approval" for n in ast.walk(ra))
    # only api_discover may mint an approval for do_discover, and only via the credential check
    ad = next(r for r in routes if r.name == "api_discover")
    assert any(isinstance(n, ast.Attribute) and n.attr == "operator_approval" for n in ast.walk(ad))
    aa = next(r for r in routes if r.name == "api_agent")
    assert not any(isinstance(n, ast.Attribute) and "approval" in n.attr and n.attr != "_strip_approval"
                   for n in ast.walk(aa))
