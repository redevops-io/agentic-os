"""The browser console's static files: served with a strict policy, free of the constructs that defeat it, and reachable before sign-in
(they carry no data). The behaviour of the console itself is exercised by the Playwright run against a real stack (deploy/standalone/
edge-sentinel/e2e)."""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

app_mod = importlib.import_module("edge-sentinel.app")
ui_routes = importlib.import_module("edge-sentinel.ui_routes")
UI = Path(__file__).resolve().parent / "ui"
EXPECTED_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


@pytest.fixture()
def gated(monkeypatch, tmp_path):
    for k in ("PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SENTINEL_REQUIRE_AUTH", "1")
    monkeypatch.setenv("SENTINEL_UI", "1")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", "admin")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", "admin-pass-for-tests-1")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "s"))
    live = importlib.import_module("edge-sentinel.live")
    live.reset_store()
    return TestClient(app_mod.app, follow_redirects=False)


def test_the_console_files_are_served_before_sign_in_with_the_exact_strict_policy(gated):
    for path, ctype in (("/ui/", "text/html"), ("/ui/app.js", "text/javascript"), ("/ui/app.css", "text/css"), ("/ui/icon.svg", "image/svg+xml"),
                        ("/ui/cases/case-abc", "text/html"), ("/ui/users", "text/html")):
        r = gated.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype), path
        assert r.headers["content-security-policy"] == EXPECTED_CSP, path
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert gated.get("/ui/secrets.env").status_code == 404 and gated.get("/ui/..%2fapp.py").status_code in (200, 404)
    assert gated.get("/ui/app.py").status_code == 404 and "def " not in gated.get("/ui/%2e%2e/app.py").text


def test_the_front_page_redirects_to_the_console_only_when_it_is_on(gated, monkeypatch):
    r = gated.get("/")
    assert r.status_code == 302 and r.headers["location"] == "/ui/"                      # reachable without a credential: it only redirects
    monkeypatch.delenv("SENTINEL_UI")
    assert gated.get("/").status_code == 401                                              # the legacy page still needs sign-in in the standalone profile
    monkeypatch.delenv("SENTINEL_REQUIRE_AUTH")
    monkeypatch.setattr(app_mod, "fetch_activity", lambda: importlib.import_module("edge-sentinel.core").fetch_activity())
    assert "Edge Sentinel" in TestClient(app_mod.app).get("/").text                       # reference profile: unchanged legacy HTML


def test_only_the_listed_files_exist_and_each_is_served():
    assert {p.name for p in UI.iterdir()} == set(ui_routes.FILES)


def test_the_console_has_no_inline_script_inline_handler_eval_innerhtml_or_third_party_url():
    for p in UI.iterdir():
        text = p.read_text()
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function|setTimeout\(\s*[\"']|\son[a-z]+\s*=", text), p.name
        assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", text), f"{p.name}: an absolute URL"
        assert "@import" not in text and "url(" not in text
    html = (UI / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "<style" not in html and ' style="' not in html
    assert not re.search(r"\bstyle=|setAttribute\(\s*[\"']style", (UI / "app.js").read_text() + (UI / "dom.js").read_text())
