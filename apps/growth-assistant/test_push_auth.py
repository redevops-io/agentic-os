"""Live pushes from /agent/run need an authenticated operator AND an explicit push flag; anonymous only generates."""
from __future__ import annotations

import base64
import importlib

import pytest
from fastapi.testclient import TestClient

core = importlib.import_module("growth-assistant.core")
ga_app = importlib.import_module("growth-assistant.app")

CRED = "operator:test-operator-pass"
TARGETS = [{"company": "Acme", "email": "ceo@acme.test", "name": "Pat"}]
BODY = {"action": "cold_outreach", "startup": {"name": "CIWatch"}, "targets": TARGETS}


def _basic(cred):
    return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload or {}

    def json(self):
        return self._p


class _Client:
    def __init__(self, http):
        self._h = http

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self._h.posts.append(url)
        return _Resp(201, {"data": {"name": "LEAD-1"}})


class _HTTP:
    def __init__(self):
        self.posts = []

    def get(self, *a, **k):
        return _Resp(200)

    def post(self, url, **k):
        self.posts.append(url)
        return _Resp(200)

    def Client(self, timeout=None):
        return _Client(self)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    http = _HTTP()
    monkeypatch.setattr(core, "httpx", http)
    monkeypatch.setattr(core, "ASSET_DIR", tmp_path)
    monkeypatch.setattr(core, "GENERATION_ONLY", False)
    monkeypatch.setattr(core, "ERPNEXT_API_KEY", "k")
    monkeypatch.setattr(core, "ERPNEXT_API_SECRET", "s")
    monkeypatch.setattr(ga_app, "_gen_json", lambda *a, **k: {"loom_audit_script": "x"})
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", CRED)
    monkeypatch.delenv("OUTREACH_BASIC_AUTH", raising=False)
    return http, TestClient(ga_app.app)


def test_generation_only_defaults_on():
    import re
    src = open(core.__file__).read()
    assert re.search(r'environ\.get\("GENERATION_ONLY",\s*"true"\)', src)


@pytest.mark.parametrize("headers", [{}, _basic("operator:wrong"), {"Authorization": "Bearer x"}])
def test_anonymous_or_wrong_credential_never_pushes(env, headers):
    http, c = env
    r = c.post("/agent/run", json={**BODY, "push": True, "_approval": {"approved": True}}, headers=headers).json()
    assert r["status"] == "done" and r["asset_id"]
    assert not r.get("pushed")
    assert http.posts == []


def test_operator_without_explicit_push_does_not_push(env):
    http, c = env
    r = c.post("/agent/run", json=BODY, headers=_basic(CRED)).json()
    assert not r.get("pushed") and http.posts == []


def test_operator_with_push_writes(env):
    http, c = env
    r = c.post("/agent/run", json={**BODY, "push": True}, headers=_basic(CRED)).json()
    assert r["pushed"]["erpnext"]["leads_created"] == ["LEAD-1"]
    assert len(http.posts) == 1


def test_generation_only_blocks_even_the_operator(env, monkeypatch):
    http, c = env
    monkeypatch.setattr(core, "GENERATION_ONLY", True)
    r = c.post("/agent/run", json={**BODY, "push": True}, headers=_basic(CRED)).json()
    assert not r.get("pushed") and http.posts == []


def test_no_configured_credential_means_nobody_is_operator(env, monkeypatch):
    http, c = env
    monkeypatch.delenv("OPERATOR_BASIC_AUTH")
    r = c.post("/agent/run", json={**BODY, "push": True}, headers=_basic(CRED)).json()
    assert not r.get("pushed") and http.posts == []
