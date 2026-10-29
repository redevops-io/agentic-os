"""The control-plane dashboard asks for the fleet credential and keeps it in the page's memory only.

The gated reads it makes (``GET /api/fleet`` and ``GET /approvals``) need the fleet key or the control token. The page therefore has an explicit credential field:
a password input that the browser is told not to remember; the value lives in one closure; it is sent only as a request header (``X-API-Key`` or
``Authorization: Bearer``) to the same origin; it is never put in browser storage, a cookie, a URL, a log or the page (the field is emptied and the status line
never repeats it); a refusal drops it and never shows the response body; it is cleared when the page goes away. There is no weaker path: without a credential the
page makes no request at all and says what it needs.

Static checks on the page source, behaviour checks by running its script against a minimal DOM stub with node (tests/_dashboard_js_harness.js, no browser, no
network), and the response headers of ``GET /``.
"""
from __future__ import annotations

import html.parser
import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_HAS_NODE = bool(shutil.which("node"))
_REQUIRED = os.environ.get("AGENTIC_OS_REQUIRE_NODE_TESTS") == "1"        # set by the CI workflow: there a missing node is a FAILURE, not a skip
needs_node = pytest.mark.skipif(not _HAS_NODE and not _REQUIRED, reason="node not installed (set AGENTIC_OS_REQUIRE_NODE_TESTS=1 to make that a failure)")
SECRET = "SYNTH-DASH-CRED-12345-DO-NOT-STORE"
BODY_SECRET = "SYNTH-BODY-LEAK-DO-NOT-STORE"


@pytest.fixture(autouse=True)
def _cp_env(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("cpdash")
    monkeypatch.setenv("PERMISSIONS_DIR", str(d / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(d / "home"))


def _page():
    from agentic_os import control_plane
    return control_plane.DASHBOARD_HTML


def _script(page):
    return "\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", page))


class _Tags(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


# ── static checks on the source ───────────────────────────────────────────────────────────────
def test_the_script_uses_no_browser_storage_cookie_url_or_log():
    js = _script(_page())
    for api in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "caches.", "openDatabase"):
        assert api not in js, api
    for api in ("location.hash", "location.search", "location.href", "location.assign", "location.replace", "history.pushState", "history.replaceState",
                "window.name", "URLSearchParams", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "postMessage", "window.open"):
        assert api not in js, api
    assert "console." not in js and "alert(" not in js and "debugger" not in js


def test_every_request_goes_through_one_helper_with_a_literal_same_origin_path():
    js = _script(_page())
    fetches = re.findall(r"\bfetch\(([^)]*)\)", js)
    assert len(fetches) == 1 and fetches[0].startswith("path,"), fetches                      # only apiGet() fetches, and with the credential headers
    gets = re.findall(r"await apiGet\(([^)]*)\)", js)
    assert sorted(gets) == ['"/api/fleet"', '"/approvals"'], gets                               # literal paths: the credential can never be put into a URL
    assert js.count("credential.headers()") == 1
    assert 'credentials: "omit"' in js and 'cache: "no-store"' in js and 'referrerPolicy: "no-referrer"' in js


def test_the_credential_is_held_in_a_closure_and_never_written_anywhere_else():
    js = _script(_page())
    assert "const credential = (() => {" in js
    # it is only ever read through credential.headers() / present(), and only set from the field via useCredential()
    assert len(re.findall(r"credential\.set\(", js)) == 1
    uses = re.findall(r"\bvalue\b[^;\n]*", js.split("function useCredential", 1)[1].split("function clearCredential", 1)[0])
    assert not any(".textContent" in u or "innerHTML" in u for u in uses)
    # error and state text is fixed: it never interpolates the credential or a response body
    for fn in ("refused", "showFleetPrompt", "showApprovalsPrompt", "setCredState"):
        body = re.split(r"\n(?:async )?function ", js.split("function " + fn, 1)[1], maxsplit=1)[0]
        body = re.sub(r"//[^\n]*", "", body)
        assert "credential." not in body.replace("credential.clear()", "") and "r.text" not in body and "r.json" not in body, fn
    assert "r.text()" not in js


def test_the_field_is_a_password_input_that_asks_the_browser_not_to_remember_it():
    p = _Tags()
    p.feed(_page())
    fields = [a for t, a in p.tags if t == "input"]
    assert len(fields) == 1
    f = fields[0]
    assert f["id"] == "credValue" and f["type"] == "password" and f.get("autocomplete") == "off" and "name" not in f and "value" not in f
    assert f.get("spellcheck") == "false"


def test_the_credential_is_cleared_when_the_page_goes_away():
    js = _script(_page())
    for ev in ("pagehide", "beforeunload"):
        assert re.search(rf'window\.addEventListener\("{ev}", \(\) => {{ credential\.clear\(\); wipeCredentialField\(\); }}\)', js), ev


@needs_node
def test_node_is_available_when_it_is_required():
    if _REQUIRED:
        assert _HAS_NODE, "AGENTIC_OS_REQUIRE_NODE_TESTS=1 but node is not installed: the dashboard behaviour tests would silently not run"


# ── behaviour, by running the script ───────────────────────────────────────────────────────────
def _run(tmp_path, responses, steps):
    (tmp_path / "page.html").write_text(_page())
    (tmp_path / "sc.json").write_text(json.dumps({"responses": responses, "steps": steps}))
    p = subprocess.run(["node", str(ROOT / "tests" / "_dashboard_js_harness.js"), str(tmp_path / "page.html"), str(tmp_path / "sc.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-600:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    return out


FLEET = [{"name": "demo", "repo": "org/demo", "pain": "p", "tagline": "t", "agents": ["a"], "approval_required": [], "deploy": "compose", "port": 1, "health": "up",
          "core": "lago", "connected": True, "has_agent": True, "group": "Money"}]
OK = {"/api/fleet": {"status": 200, "body": FLEET}, "/approvals": {"status": 200, "body": [{"id": "ap1", "module": "billing", "summary": "refund", "findings": []}]}}


def _everything(out):
    """Everything the page showed after the credential was used (the snapshot right after typing still holds what the user typed into the field, by design)."""
    shown = [sn for sn in out["snapshots"] if not sn["step"].startswith('{"type"')]
    return json.dumps(shown) + " ".join(out["console"])


@needs_node
def test_without_a_credential_the_page_makes_no_request_and_says_what_it_needs(tmp_path):
    out = _run(tmp_path, OK, [{"tick": 1}, {"tick": 1}])
    assert out["calls"] == [] and out["storageTouched"] is False
    last = out["snapshots"][-1]
    assert "fleet credential" in last["groups"]["text"] and "fleet credential" in last["approvals"]["text"] and "credential needed" in last["summary"]["text"]


@needs_node
@pytest.mark.parametrize("kind,header", [("dashboard", "X-Dashboard-Token"), ("key", "X-API-Key"), ("bearer", "Authorization")])
def test_the_credential_is_sent_only_as_a_header_and_is_never_echoed_back(tmp_path, kind, header):
    out = _run(tmp_path, OK, [{"select": ["credKind", kind]}, {"type": ["credValue", SECRET]}, {"click": "credUse"}])
    assert [c["url"] for c in out["calls"]] == ["/api/fleet", "/approvals"]                    # literal paths, no query string, no fragment
    want = "Bearer " + SECRET if kind == "bearer" else SECRET
    for c in out["calls"]:
        assert c["headers"] == {header: want} and c["credentials"] == "omit" and c["cache"] == "no-store"
        assert SECRET not in c["url"]
    after = out["snapshots"][-1]
    assert after["credValue"]["value"] == ""                                                    # the field was emptied
    assert SECRET not in _everything(out) and out["console"] == [] and out["storageTouched"] is False
    assert "demo" in after["groups"]["text"] and "refund" in after["approvals"]["text"]


@needs_node
def test_enter_in_the_field_uses_the_credential(tmp_path):
    out = _run(tmp_path, OK, [{"type": ["credValue", SECRET]}, {"key": ["credValue", "Enter"]}])
    assert len(out["calls"]) == 2 and SECRET not in _everything(out)


@needs_node
def test_an_empty_field_does_nothing(tmp_path):
    out = _run(tmp_path, OK, [{"type": ["credValue", "   "]}, {"click": "credUse"}])
    assert out["calls"] == [] and "Enter a credential" in out["snapshots"][-1]["credState"]["text"]


@needs_node
def test_a_refused_credential_is_discarded_and_no_secret_or_body_is_shown(tmp_path):
    refused = {"/api/fleet": {"status": 401, "body": {"detail": BODY_SECRET}}, "/approvals": {"status": 401, "body": {"detail": BODY_SECRET}}}
    out = _run(tmp_path, refused, [{"type": ["credValue", SECRET]}, {"click": "credUse"}, {"tick": 1}, {"tick": 1}])
    n_after_use = out["snapshots"][2]["calls"]
    assert out["calls"][0]["headers"] == {"X-API-Key": SECRET}
    assert out["snapshots"][-1]["calls"] == n_after_use, "after a 401 the credential is dropped, so later polls make no request"
    text = _everything(out)
    assert SECRET not in text and BODY_SECRET not in text
    assert "not accepted" in out["snapshots"][-1]["credState"]["text"] or "not accepted" in out["snapshots"][2]["credState"]["text"]


@needs_node
def test_a_503_says_the_server_has_no_credential_configured_and_shows_no_body(tmp_path):
    disabled = {"/api/fleet": {"status": 503, "body": {"detail": BODY_SECRET}}, "/approvals": {"status": 503, "body": {"detail": BODY_SECRET}}}
    out = _run(tmp_path, disabled, [{"type": ["credValue", SECRET]}, {"click": "credUse"}])
    last = out["snapshots"][-1]
    assert "no fleet credential configured" in last["credState"]["text"] and "disabled until" in last["groups"]["text"]
    assert SECRET not in _everything(out) and BODY_SECRET not in _everything(out)


@needs_node
@pytest.mark.parametrize("end", [{"click": "credClear"}, {"event": "pagehide"}, {"event": "beforeunload"}])
def test_clear_and_page_unload_drop_the_credential(tmp_path, end):
    out = _run(tmp_path, OK, [{"type": ["credValue", SECRET]}, {"click": "credUse"}, end, {"tick": 1}])
    at_end = out["snapshots"][-2]["calls"]
    assert out["snapshots"][-1]["calls"] == at_end, "after the credential is dropped, polling makes no request at all"
    assert out["snapshots"][-1]["credValue"]["value"] == "" and SECRET not in _everything(out)


@needs_node
def test_after_pagehide_the_next_poll_sends_no_credential(tmp_path):
    out = _run(tmp_path, OK, [{"type": ["credValue", SECRET]}, {"click": "credUse"}, {"event": "pagehide"}])
    n = out["snapshots"][-1]["calls"]
    out2 = _run(tmp_path, OK, [{"type": ["credValue", SECRET]}, {"click": "credUse"}, {"event": "pagehide"}, {"tick": 1}])
    assert out2["snapshots"][-1]["calls"] == n, "polls after pagehide make no request"
    assert "fleet credential" in out2["snapshots"][-1]["groups"]["text"]


# ── the response ──────────────────────────────────────────────────────────────────────────────
def test_the_dashboard_response_cannot_be_framed_cached_and_has_the_connect_policy():
    from fastapi.testclient import TestClient
    from agentic_os import control_plane
    r = TestClient(control_plane.app).get("/")
    assert r.status_code == 200 and r.text == control_plane.DASHBOARD_HTML
    h = r.headers
    assert h["cache-control"] == "no-store" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer" and h["x-content-type-options"] == "nosniff"
    csp = h["content-security-policy"]
    # exactly what the policy does and does not do (see the comment in control_plane.py): connect-src 'self' limits fetch/XHR/WebSocket/EventSource/beacon to this origin;
    # there is deliberately no script-src, so this is not a defence against script injection.
    assert "script-src" not in csp and "default-src" not in csp
    for d_ in ("frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'", "object-src 'none'", "connect-src 'self'"):
        assert d_ in csp, d_


def test_the_read_only_dashboard_token_is_the_default_choice_and_the_other_two_stay_available():
    p = _Tags()
    p.feed(_page())
    opts = re.findall(r'<option value="([^"]+)">([^<]+)</option>', _page().split('id="credKind"', 1)[1].split("</select>", 1)[0])
    assert [v for v, _ in opts] == ["dashboard", "key", "bearer"]
    assert "X-Dashboard-Token" in opts[0][1] and "read-only" in opts[0][1].lower()
    js = _script(_page())
    assert 'let kind = "dashboard"' in js                                    # the narrow credential is what the page reaches for first
