"""PROJECTS_OPERATOR_GATE: nothing is served before the server has validated who is asking."""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app, mounted_app


def basic(user="ops", pw="pw-123456"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "OPERATOR_BASIC_AUTH",
              "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "OUTREACH_BASIC_AUTH", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_DASHBOARDS", "PROJECTS_OPERATOR_GATE"):
        monkeypatch.delenv(k, raising=False)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html>secret ui</html>")
    monkeypatch.setenv("PROJECTS_UI_DIR", str(ui))
    return monkeypatch


def test_off_by_default_nothing_changes(env):
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.get("/api/projects").status_code == 200 and c.get("/healthz").json() == {"status": "ok"}


def test_every_route_and_the_ui_need_the_operator_and_the_browser_is_asked(env):
    env.setenv("PROJECTS_OPERATOR_GATE", "1")
    env.setenv("OPERATOR_BASIC_AUTH", "ops:pw-123456")
    c = TestClient(create_app(SampleProjectionProvider()))
    for path in ("/", "/index.html", "/api/projects", "/api/dashboards", "/api/dashboards/missions", "/api/projects/customer-ops/today"):
        r = c.get(path)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith('Basic realm="Projects"') and "secret ui" not in r.text, path
    assert c.post("/api/sidekick", json={"ctx": {}, "text": "hi"}).status_code == 401
    assert c.get("/api/projects", headers=basic(pw="wrong")).status_code == 401
    assert c.get("/api/projects", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/healthz").status_code == 200                                        # liveness only
    assert c.post("/healthz").status_code == 401
    assert c.get("/api/projects", headers=basic()).status_code == 200 and "secret ui" in c.get("/", headers=basic()).text
    cat = c.get("/api/dashboards", headers=basic()).json()
    assert cat["access"]["principal"] == "operator" and not cat["access"]["scoped"]     # the same person for reads and writes
    v = c.put("/api/dashboards/views", json={"name": "mine", "dashboard": "missions"}, headers=basic())
    assert v.status_code == 200 and [x["name"] for x in c.get("/api/dashboards/views", headers=basic()).json()["views"]] == ["mine"]


def test_a_tenant_bearer_passes_the_gate_and_is_still_scoped(env):
    env.setenv("PROJECTS_OPERATOR_GATE", "1")
    env.setenv("AGENTIC_OS_TENANT_TOKENS", "qa=tok-qa-0123456789,none=tok-none-0123456789")
    env.setenv("AGENTIC_OS_TENANT_LIMITS", '{"qa": {"policy_refs": ["dashboards:missions", "project:customer-ops"]}}')
    c = TestClient(create_app(SampleProjectionProvider()))
    qa, none = {"Authorization": "Bearer tok-qa-0123456789"}, {"Authorization": "Bearer tok-none-0123456789"}
    assert {d["id"] for d in c.get("/api/dashboards", headers=qa).json()["dashboards"]} == {"portfolio", "missions"}
    assert c.get("/api/dashboards", headers=none).json()["dashboards"] == []            # through the gate, and still granted nothing
    assert c.get("/api/dashboards", headers={"Authorization": "Bearer tok-other"}).status_code == 401


def test_the_gate_fails_closed_without_any_credential_and_holds_under_a_base_path(env):
    env.setenv("PROJECTS_OPERATOR_GATE", "1")
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.get("/api/projects").status_code == 503 and c.get("/api/projects", headers=basic()).status_code == 503
    env.setenv("OPERATOR_BASIC_AUTH", "ops:pw-123456")
    m = TestClient(mounted_app("/projects", SampleProjectionProvider()))
    assert m.get("/projects/api/projects").status_code == 401 and m.get("/projects/").status_code == 401
    assert m.get("/projects/healthz").status_code == 200
    assert m.get("/projects/api/projects", headers=basic()).status_code == 200
    two = [("Authorization", basic()["Authorization"]), ("Authorization", basic()["Authorization"])]
    assert m.get("/projects/api/projects", headers=two).status_code == 401              # a repeated header is refused


def test_the_junit_reporter_can_pass_the_gate_without_weakening_the_service_identity(monkeypatch, tmp_path):
    """Behind the gate a reporter needs an Authorization the gate accepts; the service token is still what authorises the report."""
    import io
    import json as _json

    from agentic_os.dashboards import report_junit

    f = tmp_path / "r.xml"
    f.write_text('<testsuite><testcase classname="a" name="t"/></testsuite>')
    seen = []

    def fake(req, timeout=0):
        seen.append({k.lower(): v for k, v in req.header_items()})
        return io.BytesIO(_json.dumps({"ok": True}).encode())

    monkeypatch.setattr(report_junit.urllib.request, "urlopen", fake)
    monkeypatch.setenv("DASHBOARDS_SERVICE_TOKEN", "svc-token")
    argv = [str(f), "--url", "http://x", "--project", "p", "--suite", "s", "--run-id", "1"]
    monkeypatch.delenv("DASHBOARDS_AUTHORIZATION", raising=False)
    assert report_junit.main(argv) == 0 and "authorization" not in seen[-1] and seen[-1]["x-service-token"] == "svc-token"
    monkeypatch.setenv("DASHBOARDS_AUTHORIZATION", "Bearer gate-credential")
    assert report_junit.main(argv) == 0
    assert seen[-1]["authorization"] == "Bearer gate-credential" and seen[-1]["x-service-token"] == "svc-token"
