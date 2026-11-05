"""The public /about/ routes behind the local capability gate (agentic_os/about_ui.py).

The gate stays deny-by-default: only the exact About paths are added to the unauthenticated GET surface; every other /about/* path,
every other method and every other route is unchanged. Offline; the Licenses folder is built by the real generator from synthetic
inputs (tests/about_fixture.py).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agentic_os.about_ui as about_ui
from agentic_os.local_capability import LocalCapability
from agentic_os.projects_api import SampleProjectionProvider, mounted_app

from .about_fixture import about_record, decide, make_notices, make_root, read_gate, run_generate, stage_folder

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "agentic_os" / "about_assets"
EXPECTED_PUBLIC = {"/about/", "/about/app.js", "/about/app.css", "/about/about.json"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AGENT_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)


@pytest.fixture()
def folder(tmp_path):
    root = make_root(tmp_path)
    notices = make_notices(tmp_path)
    run_generate(root, notices)
    return stage_folder(root, notices, tmp_path / "Licenses")


def app_for(licenses: Path | None, monkeypatch, base: str = ""):
    if licenses is None:
        monkeypatch.setattr(about_ui, "find_licenses_dir", lambda: None)
    else:
        monkeypatch.setenv("AGENTIC_OS_LICENSES_DIR", str(licenses))
    cap = LocalCapability(port=PORT)
    app = mounted_app(base, SampleProjectionProvider(), capability=cap)
    return TestClient(app, base_url=BASE_URL)


def slugs(folder: Path):
    d = json.loads((folder / "about.json").read_bytes())
    return [f["id"] for f in d["licence_files"]] + ["notices"]


def raw_get(app, path: str, method: str = "GET", host: str = "127.0.0.1:8787"):
    """Call the ASGI app with an UNNORMALISED, already percent-decoded path (an HTTP client collapses `..` before sending)."""
    import asyncio
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", host.encode())], "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8787)}
    out = {"status": None, "headers": [], "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"], out["headers"] = msg["status"], msg["headers"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
    asyncio.run(app(scope, receive, send))
    return out


def no_cors(r):
    return [k for k in r.headers if k.lower().startswith("access-control-")] == []


# ----------------------------------------------------------------------------------------------------------- public surface
def test_exact_about_paths_are_public_without_a_session(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    for path in sorted(EXPECTED_PUBLIC | {f"/about/files/{s}" for s in slugs(folder)}):
        for method in ("GET", "HEAD"):
            r = c.request(method, path)
            assert r.status_code == 200, (method, path, r.status_code)
            assert no_cors(r) and r.headers["cross-origin-resource-policy"] == "same-origin"
            assert r.headers["x-content-type-options"] == "nosniff" and r.headers["content-security-policy"] == CSP
            assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"
            assert r.headers["cache-control"] == "no-cache" and "etag" in r.headers
            assert "set-cookie" not in r.headers


def test_the_public_set_is_exactly_the_documented_one(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    pub = c.app.state.local_public_paths if hasattr(c.app.state, "local_public_paths") else None
    if pub is None:                                   # base path: the inner app holds it
        pub = c.app.routes[0].app.state.local_public_paths
    about = {p for p in pub if p.startswith("/about")}
    assert about == EXPECTED_PUBLIC | {f"/about/files/{s}" for s in slugs(folder)}
    # the pre-existing public surface is unchanged
    assert {"/", "/index.html", "/local-capability-shim.js"} <= set(pub)


@pytest.mark.parametrize("path", [
    "/about", "/about/x", "/about/files/", "/about/files/nope", "/about/files/..",
    "/about/files/%2e%2e/about.json", "/about/files/%2e%2e%2fabout.json", "/about/files/licence/", "/about/files/LICENSE",
    "/about/files/licence%00", "/about/files//etc/passwd", "/about/files/..%5cabout.json", "/about/files/C:/Windows/win.ini",
    "/about/../api/projects", "/about//about.json", "/about/app.js/", "/about/APP.JS", "/about/about.json/", "/about/.env",
    "/about/files/%2Fetc%2Fpasswd", "/about/index.html", "/about/about.html",
    "/about/files/licence?x=../../etc/passwd#",
])
def test_unknown_and_traversal_paths_stay_401(folder, monkeypatch, path):
    c = app_for(folder, monkeypatch)
    r = c.get(path, follow_redirects=False)
    if path.endswith("?x=../../etc/passwd#"):
        assert r.status_code == 200                    # the query string is not part of the path: still the exact licence file
        return
    assert r.status_code in (401, 400, 404), (path, r.status_code)
    assert r.status_code != 200 or path in EXPECTED_PUBLIC
    if r.status_code == 401:
        assert r.json()["error"]["code"] == "auth_required"
    assert not r.content.startswith(b"root:") and no_cors(r)


@pytest.mark.parametrize("path", [
    "/about/files/../about.json", "/about/files/notices/../licence", "/about/files/..", "/about/../api/projects", "/about/files/../../etc/passwd",
    "/about/files/./licence", "/about/files/licence/.", "/about/files/\\..\\x", "/about/files/..\\about.json", "/about/files/\x00",
    "/about/files/licence\x00.txt", "/about/files/%2e%2e/about.json", "/about/files/licence ", "/about/files/ licence", "/about/files/LICENCE",
    "/about/files/c:\\windows\\win.ini", "/about/files//licence", "///about/about.json", "/about/files/licence;x", "/about/%2e%2e/about.json",
])
def test_decoded_traversal_paths_reach_the_gate_unnormalised_and_get_401(folder, monkeypatch, path):
    c = app_for(folder, monkeypatch)
    out = raw_get(c.app, path)
    assert out["status"] == 401, (path, out["status"], out["body"][:80])
    assert b"auth_required" in out["body"] and not any(k.lower().startswith(b"access-control-") for k, _ in out["headers"])


def test_the_raw_helper_reaches_a_real_public_path(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    assert raw_get(c.app, "/about/about.json")["status"] == 200
    assert raw_get(c.app, "/about/files/licence", "HEAD")["status"] == 200
    assert raw_get(c.app, "/about/files/licence", host="evil.test")["status"] == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", sorted(EXPECTED_PUBLIC))
def test_no_other_method_reaches_the_pages(folder, monkeypatch, method, path):
    c = app_for(folder, monkeypatch)
    r = c.request(method, path, content=b"{}")
    assert r.status_code in (401, 403) and no_cors(r), (method, path, r.status_code)
    assert r.headers["cache-control"] == "no-store"


def test_cross_origin_requests_get_no_cors_headers_and_preflight_is_refused(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    r = c.get("/about/about.json", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200 and no_cors(r)                  # readable only same-origin: CORP + no ACAO
    r = c.options("/about/about.json", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 403 and no_cors(r)


def test_dns_rebinding_host_is_refused(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    r = c.get("/about/", headers={"Host": "evil.test:8787"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_host"


def test_the_api_stays_gated(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    assert c.get("/api/projects").status_code == 401
    assert c.post("/api/sources/confirm", json={}).status_code == 401
    assert c.get("/docs").status_code in (401, 404)


def test_mounted_base_path_serves_the_same_pages(folder, monkeypatch):
    c = app_for(folder, monkeypatch, base="/projects")
    assert c.get("/projects/about/").status_code == 200
    assert c.get("/projects/about/about.json").status_code == 200
    assert c.get("/projects/about/files/licence").status_code == 200
    assert c.get("/projects/about/files/nope").status_code == 401
    assert c.get("/about/").status_code == 401                  # outside the base path nothing is public


# ----------------------------------------------------------------------------------------------------------- bodies
def test_served_files_are_byte_exact_and_etag_is_their_sha256(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    doc = json.loads((folder / "about.json").read_bytes())
    for f in doc["licence_files"] + [doc["notices"]]:
        slug = f.get("id", "notices")
        r = c.get(f"/about/files/{slug}")
        assert r.content == (folder / f["file"]).read_bytes()
        assert r.headers["content-type"].startswith("text/plain") and r.headers["etag"] == f'"{f["sha256"]}"'
    r = c.get("/about/about.json")
    assert r.content == (folder / "about.json").read_bytes() and r.headers["content-type"] == "application/json"
    assert r.headers["etag"] == '"%s"' % hashlib.sha256(r.content).hexdigest()
    again = c.get("/about/about.json", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


def test_media_types(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    assert c.get("/about/").headers["content-type"].startswith("text/html")
    assert c.get("/about/app.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/about/app.css").headers["content-type"].startswith("text/css")


def test_page_and_pages_make_no_cross_origin_reference():
    html = (ASSETS / "about.html").read_text()
    css = (ASSETS / "app.css").read_text()
    js = (ASSETS / "app.js").read_text()
    assert not re.search(r"(?i)\b(?:https?:)?//(?!\s)[\w.-]+\.[a-z]{2,}", html + css), "no external reference in html/css"
    assert "url(" not in css and "@import" not in css
    # CSP-clean: no inline script, no inline handler, external files only
    assert "<script>" not in html and not re.search(r"(?i)\son[a-z]+\s*=", html)
    assert re.findall(r'<script src="([^"]+)"', html) == ["app.js"]
    # text only: nothing from the record or from a file reaches the DOM as markup
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\"",
                   "javascript:", "XMLHttpRequest", "WebSocket", "sendBeacon", "localStorage", "sessionStorage", "/api/"):
        assert banned not in js, banned
    assert re.findall(r'fetch\("([^"]+)', js) == ["./about.json", "./files/"] and 'fetch("./files/" + slug' in js
    assert 'type="module"' not in html and "src=\"http" not in html


# ----------------------------------------------------------------------------------------------------------- states
def test_no_record_gives_404_but_the_page_still_answers(monkeypatch):
    c = app_for(None, monkeypatch)
    assert c.get("/about/").status_code == 200
    r = c.get("/about/about.json")
    assert r.status_code == 404 and r.json() == {"error": {"code": "ABOUT_NO_RECORD"}}
    assert c.get("/about/files/licence").status_code == 401     # no file is registered, so it is not even public


@pytest.mark.parametrize("what,code", [
    ("record", about_ui.ERR_INVALID), ("file", about_ui.ERR_MISMATCH), ("manifest", about_ui.ERR_MANIFEST),
    ("manifest-hash", about_ui.ERR_MISMATCH), ("missing-file", about_ui.ERR_MISMATCH), ("truncated", about_ui.ERR_MISMATCH),
])
def test_a_record_that_fails_verification_serves_a_fixed_error_and_no_files(folder, monkeypatch, what, code):
    if what == "record":
        (folder / "about.json").write_text('{"schema": "redevops.about/1"}')
    elif what == "file":
        (folder / "LICENSE").write_text("tampered")
    elif what == "manifest":
        (folder / "LICENSES-MANIFEST.json").unlink()
    elif what == "manifest-hash":
        m = json.loads((folder / "LICENSES-MANIFEST.json").read_text())
        m["files"]["about.json"]["sha256"] = "0" * 64
        (folder / "LICENSES-MANIFEST.json").write_text(json.dumps(m))
    elif what == "missing-file":
        (folder / "LICENSING.md").unlink()
    elif what == "truncated":
        (folder / "THIRD_PARTY_NOTICES.txt").write_bytes((folder / "THIRD_PARTY_NOTICES.txt").read_bytes()[:-5])
    c = app_for(folder, monkeypatch)
    if what in ("record",):
        # a record edited by hand is also not in the manifest: either way it is refused before any file is served
        pass
    r = c.get("/about/about.json")
    assert r.status_code == 200 and r.json() == {"schema": "redevops.about/1", "error": {"code": code}}
    assert len(r.content) < 100
    for s in ("licence", "notices", "licensing"):
        assert c.get(f"/about/files/{s}").status_code == 401
    assert c.get("/about/").status_code == 200


def test_oversize_record_is_refused(folder, monkeypatch):
    (folder / "about.json").write_bytes(b" " * (about_record.MAX_BYTES_READ + 1))
    c = app_for(folder, monkeypatch)
    assert c.get("/about/about.json").json()["error"]["code"] == about_ui.ERR_INVALID


def test_served_file_sizes_are_capped(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    notices = make_notices(tmp_path)
    run_generate(root, notices)
    assert about_record.MAX_SERVED_FILE_BYTES == 4 * 1024 * 1024
    doc = json.loads((notices / "about.json").read_bytes())
    doc["notices"]["bytes"] = about_record.MAX_SERVED_FILE_BYTES + 1
    assert about_record.validate_record(doc)


def test_disabled_entry_registers_nothing(folder, monkeypatch):
    monkeypatch.setattr(about_ui, "ENTRY_ENABLED", False)
    c = app_for(folder, monkeypatch)
    assert c.get("/about/").status_code == 401


def test_files_are_served_from_memory_so_a_swap_after_start_changes_nothing(folder, monkeypatch):
    c = app_for(folder, monkeypatch)
    before = c.get("/about/files/licence").content
    (folder / "LICENSE").write_text("swapped after start")
    assert c.get("/about/files/licence").content == before


def test_frozen_sidecar_ignores_the_environment_override(folder, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path / "nowhere"), raising=False)
    monkeypatch.setenv("AGENTIC_OS_LICENSES_DIR", str(folder))
    assert about_ui.find_licenses_dir() is None
    (tmp_path / "nowhere" / "Licenses").mkdir(parents=True)
    (tmp_path / "nowhere" / "Licenses" / "about.json").write_text("{}")
    assert about_ui.find_licenses_dir() == tmp_path / "nowhere" / "Licenses"


# ----------------------------------------------------------------------------------------------------------- filled slots
def test_a_filled_slot_reaches_the_record_the_page_reads(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    notices = make_notices(tmp_path)
    decide(root, ["D-LIC-10"])
    assert run_generate(root, notices) == 0
    lic = stage_folder(root, notices, tmp_path / "Licenses")
    c = app_for(lic, monkeypatch)
    slot = c.get("/about/about.json").json()["legal"]["slots"]["LEGAL.NO_WARRANTY"]
    assert slot["state"] == "filled" and slot["text"].startswith("SYNTHETIC TEST TEXT")


# ----------------------------------------------------------------------------------------------------------- no new state change
def test_about_routes_are_read_only():
    src = (ROOT / "agentic_os" / "about_ui.py").read_text()
    assert 'methods=["GET", "HEAD"]' in src and "POST" not in src and "PUT" not in src and "DELETE" not in src
    assert "StaticFiles" not in src and "mount(" not in src and "glob(" not in src and "listdir" not in src


# ----------------------------------------------------------------------------------------------------------- wording
FORBIDDEN = ("open source", "cleared", "compliant", "approved by counsel", "legally")


def test_page_assets_and_generated_record_contain_no_forbidden_wording(folder):
    """The page (html/js/css), the generated about.json and the placeholder text: none of the forbidden phrases. (The shipped
    third-party notice texts and an owner-approved slot text are not product strings and are not linted.)"""
    blobs = {n: (ASSETS / n).read_text(encoding="utf-8") for n in ("about.html", "app.js", "app.css")}
    blobs["about.json"] = (folder / "about.json").read_text(encoding="utf-8")
    for name, text in blobs.items():
        low = text.lower()
        assert not [w for w in FORBIDDEN if w in low], name
    doc = json.loads(blobs["about.json"])
    for sid, s in doc["legal"]["slots"].items():
        assert s["state"] == "placeholder" and "private candidate" in s["text"] and "pending" in s["text"], sid
    # no product licence statement is typed into the page: the family-licence phrase and the SPDX name appear nowhere in the assets
    for name in ("about.html", "app.js", "app.css"):
        assert "family licence" not in blobs[name].lower() and "agpl" not in blobs[name].lower() and "commons clause" not in blobs[name].lower().replace("commons clause file", "")
