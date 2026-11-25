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
              "OPERATOR_BASIC_AUTH_USER", "OPERATOR_BASIC_AUTH_PASS", "OUTREACH_BASIC_AUTH", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_DASHBOARDS", "PROJECTS_OPERATOR_GATE",
              "PROJECTS_CORS_ORIGINS"):
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


def test_behind_the_gate_no_file_is_marked_for_a_shared_cache(monkeypatch, tmp_path):
    """0.4.12 marked the UI's hashed files "public": the edge in front of a gated deployment then served them to anonymous
    callers without asking the server. Behind the gate a file may be kept by the signed-in person's own browser only."""
    import base64

    from fastapi.testclient import TestClient
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html><script src='assets/index-AbCd1234.js'></script></html>")
    (ui / "assets" / "index-AbCd1234.js").write_text("console.log(1)")
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_DASHBOARDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PROJECTS_UI_DIR", str(ui))
    monkeypatch.setenv("OPERATOR_BASIC_AUTH", "operator:" + "p" * 40)
    monkeypatch.setenv("PROJECTS_OPERATOR_GATE", "1")
    from agentic_os.projects_api import create_app
    c = TestClient(create_app())
    h = {"Authorization": "Basic " + base64.b64encode(("operator:" + "p" * 40).encode()).decode()}
    assert c.get("/assets/index-AbCd1234.js").status_code == 401                     # the gate, not a cache, answers an anonymous caller
    cc = c.get("/assets/index-AbCd1234.js", headers=h).headers["cache-control"]
    assert cc.startswith("private") and "public" not in cc and "immutable" in cc
    assert "public" not in c.get("/", headers=h).headers["cache-control"]
    monkeypatch.delenv("PROJECTS_OPERATOR_GATE")
    open_ = TestClient(create_app())
    assert open_.get("/assets/index-AbCd1234.js").headers["cache-control"].startswith("public")   # an open deployment may be cached by anyone


def test_an_allowed_page_origin_can_ask_permission_and_read_a_refusal_but_gets_nothing_without_a_credential(env):
    """A dashboards page on another origin (the portable page) connects with a bearer. The browser's preflight carries no
    credential by specification, so from an ALLOWED origin it must be answered; nothing else changes."""
    env.setenv("PROJECTS_OPERATOR_GATE", "1")
    env.setenv("OPERATOR_BASIC_AUTH", "ops:pw-123456")
    env.setenv("PROJECTS_CORS_ORIGINS", "https://dash.example")
    c = TestClient(create_app(SampleProjectionProvider()))
    pre = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    ok = c.options("/api/dashboards", headers={"Origin": "https://dash.example", **pre})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://dash.example" and "dashboards" not in ok.text
    other = c.options("/api/dashboards", headers={"Origin": "https://evil.example", **pre})
    assert other.status_code == 401 and "access-control-allow-origin" not in other.headers        # not allowed: refused like anything else
    assert c.options("/api/dashboards", headers={"Origin": "https://dash.example"}).status_code == 401   # OPTIONS that is not a preflight
    # the real request is gated as before; the allowed page can read the refusal and is not shown the operator's Basic prompt
    r = c.get("/api/dashboards", headers={"Origin": "https://dash.example"})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "https://dash.example" and "www-authenticate" not in r.headers
    r = c.get("/api/dashboards", headers={"Origin": "https://evil.example"})
    assert r.status_code == 401 and "access-control-allow-origin" not in r.headers and "www-authenticate" in r.headers
    good = c.get("/api/dashboards", headers={"Origin": "https://dash.example", **basic()})
    assert good.status_code == 200 and good.headers["access-control-allow-origin"] == "https://dash.example"


def test_without_an_allowed_origin_a_preflight_is_refused(env):
    env.setenv("PROJECTS_OPERATOR_GATE", "1")
    env.setenv("OPERATOR_BASIC_AUTH", "ops:pw-123456")
    c = TestClient(create_app(SampleProjectionProvider()))
    r = c.options("/api/dashboards", headers={"Origin": "https://dash.example", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 401
