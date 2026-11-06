"""Local capability gate: the desktop sidecar's loopback API must be deny-by-default.

Regression for the observed attack (audit finding 4): a web page in the user's browser made a
cross-origin anonymous POST to /api/sources/confirm and the sidecar walked a local folder. The first
tests reproduce exactly that request and prove (spy + temp dir) that nothing on disk is touched.
Subprocess tests drive the real ``serve`` entry (standalone + launcher hand-off).
"""
from __future__ import annotations

import asyncio
import hmac as _hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agentic_os.local_capability as lc
from agentic_os.local_capability import (LocalCapability, handshake_proof, mint_proof, b64url, b64url_decode)
from agentic_os.projects_api import SampleProjectionProvider, create_app, mounted_app

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
EVIL = "https://evil.example"
ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    monkeypatch.delenv("AGENT_GATEWAY_TOKEN", raising=False)


def make(base="", **cap_kw):
    clock = cap_kw.pop("clock", None) or Clock()
    cap_kw.setdefault("wall_clock", clock)          # default: both clocks tick together (tests move one)
    cap = LocalCapability(port=PORT, clock=clock, **cap_kw)
    handoff = cap.launcher_handoff("127.0.0.1", PORT, base)
    app = mounted_app(base, SampleProjectionProvider(), capability=cap)
    return app, cap, handoff, clock


def client(app, **kw):
    return TestClient(app, base_url=BASE_URL, **kw)


def login(c, code, base=""):
    """Exchange a launch code; like the UI shim, keep the session token (it is NOT a cookie) and send it
    in X-Agentic-Session on every later request of this client. Returns the CSRF token."""
    r = c.post(base + "/api/local/session", json={"code": code})
    assert r.status_code == 200, r.text
    assert "set-cookie" not in r.headers
    j = r.json()
    c.headers["X-Agentic-Session"] = j["session_token"]
    return j["csrf_token"]


def no_cors(r):
    return [k for k in r.headers if k.lower().startswith("access-control-")] == []


@pytest.fixture
def walk_spy(monkeypatch):
    seen = []
    real = os.walk

    def spy(top, *a, **k):
        seen.append(str(top))
        return real(top, *a, **k)
    monkeypatch.setattr(os, "walk", spy)
    return seen


@pytest.fixture
def secret_dir(tmp_path):
    d = tmp_path / "private-folder"
    d.mkdir()
    (d / "a.txt").write_text("secret one")
    (d / "b.md").write_text("secret two")
    return d


def confirm_body(d):
    return {"project_id": "customer-ops", "confirmed_by": "x",
            "sources": [{"kind": "files", "location": str(d), "name": "x"}]}


# --------------------------------------------------------------------------- the observed attack
def test_observed_attack_cross_origin_anonymous_confirm_is_refused_without_fs_access(
        secret_dir, walk_spy, monkeypatch):
    # origin/main refuses a `files` location outside $PROJECTS_SOURCE_ROOTS without the control token (503 when unset). Allow the
    # folder here so that what refuses the anonymous requests below is the capability gate itself, not main's source-root check.
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(secret_dir.parent))
    app, cap, hand, _ = make()
    calls = []
    import agentic_os.projects_api as pa
    real = pa.confirm_and_connect_sources
    monkeypatch.setattr(pa, "confirm_and_connect_sources", lambda *a, **k: calls.append(a) or real(*a, **k))
    c = client(app)
    r = c.post("/api/sources/confirm", json=confirm_body(secret_dir),
               headers={"Origin": EVIL, "Content-Type": "application/json"})
    assert r.status_code in (401, 403) and no_cors(r)
    assert "a.txt" not in r.text and "files" not in r.text.lower().replace("origin", "")
    assert calls == [] and walk_spy == []              # the handler never ran; nothing was walked
    # the same request WITHOUT an Origin (a non-browser local process) is also refused: no credential
    r = c.post("/api/sources/confirm", json=confirm_body(secret_dir))
    assert r.status_code == 401 and calls == [] and walk_spy == []
    # control: with a session the spy DOES see the walk, so the checks above are meaningful
    csrf = login(c, hand["launch_code"])
    r = c.post("/api/sources/confirm", json=confirm_body(secret_dir), headers={"X-Agentic-CSRF": csrf})
    assert r.status_code == 200 and calls and walk_spy


def test_allow_origins_cannot_be_combined_with_the_gate():
    with pytest.raises(ValueError):
        create_app(SampleProjectionProvider(), allow_origins=["https://x.example"], capability=LocalCapability())


# --------------------------------------------------------------------------- deny by default
def _operations(app):
    """(method, path) of every API route, from the router (not openapi.json, which a pre-existing
    annotation bug breaks on feeca065; that defect is fixed elsewhere and must not mask this test)."""
    from fastapi.routing import APIRoute
    ops, todo = [], [app]
    while todo:
        a = todo.pop()
        for r in a.routes:
            if isinstance(r, APIRoute):
                for m in r.methods - {"HEAD", "OPTIONS"}:
                    if r.include_in_schema:
                        ops.append((m, re.sub(r"\{[^}]+\}", "x", r.path)))
            elif hasattr(r, "app") and hasattr(r.app, "routes") and getattr(r, "path", None) not in (None, "", "/"):
                todo.append(r.app)
    return ops


@pytest.mark.parametrize("base", ["", "/projects"])
def test_every_route_needs_the_capability(base, monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", "gw-token-123")        # mount the conditional gateway too
    app, cap, hand, _ = make(base)
    ops = _operations(app)
    assert len(ops) >= 24, ops             # 21 API routes + 3 agent-gateway routes
    assert any("/agent-gateway/" in p for _, p in ops)
    c = client(app, raise_server_exceptions=False)
    for method, path in ops:
        r = c.request(method, base + path, json={} if method == "POST" else None,
                      headers={"Authorization": "Bearer gw-token-123"})
        assert r.status_code == 401, (method, path, r.status_code)
        assert no_cors(r)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/api/does-not-exist",
                 "/api", "/assets/../api/projects", "/%2e%2e/api/projects"):
        r = c.get(base + path)
        assert r.status_code == 401, (path, r.status_code)
    # and with a session, docs / openapi are reachable
    login(c, hand["launch_code"], base)
    assert c.get(base + "/openapi.json").status_code not in (401, 403)   # (500 on feeca065: unrelated schema bug)
    assert c.get(base + "/api/projects").status_code == 200
    assert c.get(base + "/docs").status_code == 200


def test_gateway_routes_need_the_session_even_with_the_gateway_bearer(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_TOKEN", "gw-token-123")
    app, cap, hand, _ = make()
    c = client(app)
    h = {"Authorization": "Bearer gw-token-123"}
    assert c.get("/api/agent-gateway/capabilities", headers=h).status_code == 401
    csrf = login(c, hand["launch_code"])
    assert c.get("/api/agent-gateway/capabilities", headers=h).status_code == 200
    assert c.post("/api/agent-gateway/capabilities/projects.list/invoke", headers=h, json={}).status_code == 403
    assert c.post("/api/agent-gateway/capabilities/projects.list/invoke",
                  headers={**h, "X-Agentic-CSRF": csrf}, json={}).status_code == 200


def test_only_the_static_ui_is_public():
    app, *_ = make()
    c = client(app)
    r = c.get("/")
    assert r.status_code == 200 and "/local-capability-shim.js" in r.text
    assert c.get("/index.html").status_code == 200
    assert c.get("/local-capability-shim.js").status_code == 200
    js = re.search(r'src="(/assets/[^"]+\.js)"', r.text).group(1)
    assert c.get(js).status_code == 200
    assert c.post("/", json={}).status_code == 401           # only GET/HEAD on the files


def test_public_set_excludes_dotfiles_api_and_reserved_names(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    (ui / "api").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><html><head></head><body>hi</body></html>")
    (ui / "a.js").write_text("1")
    (ui / ".secret").write_text("s")
    (ui / "openapi.json").write_text("{}")
    (ui / "api" / "x.json").write_text("{}")
    monkeypatch.setenv("PROJECTS_UI_DIR", str(ui))
    app, *_ = make()
    c = client(app)
    assert c.get("/a.js").status_code == 200
    for p in ("/.secret", "/openapi.json", "/api/x.json"):
        assert c.get(p).status_code == 401, p


def test_websocket_is_refused():
    app, *_ = make()
    sent = []

    async def run():
        async def receive():
            return {"type": "websocket.connect"}

        async def send(m):
            sent.append(m)
        await app({"type": "websocket", "path": "/api/x", "headers": [(b"host", b"127.0.0.1:8787")]},
                  receive, send)
    asyncio.run(run())
    assert sent and sent[0]["type"] == "websocket.close"


# --------------------------------------------------------------------------- CORS never
def test_no_cors_header_on_any_response_including_errors_and_preflights():
    app, cap, hand, _ = make()
    c = client(app)
    h = {"Origin": EVIL}
    reqs = [
        c.get("/", headers=h), c.get("/api/projects", headers=h), c.post("/api/sidekick", json={}, headers=h),
        c.get("/nope", headers=h), c.get("/api/projects", headers={"Host": "evil.example", **h}),
        c.options("/api/sources/confirm", headers={**h, "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "content-type"}),
        c.options("/", headers={**h, "Access-Control-Request-Method": "GET"}),
        c.options("/api/local/session", headers={**h, "Access-Control-Request-Method": "POST"}),
        c.options("/api/projects"),
    ]
    for r in reqs:
        assert no_cors(r), (r.request.url, dict(r.headers))
    pre = reqs[5]
    assert pre.status_code == 403
    # authenticated, same-origin: still no CORS headers
    login(c, hand["launch_code"])
    for r in (c.get("/api/projects"), c.get("/api/projects", headers={"Origin": BASE_URL})):
        assert r.status_code == 200 and no_cors(r)


# --------------------------------------------------------------------------- Host / Origin / Fetch metadata
@pytest.mark.parametrize("host", ["evil.example", "evil.example:8787", "127.0.0.1.evil.example:8787",
                                  "localhost:9999", "127.0.0.1", "127.0.0.1:8787@evil.example",
                                  "0.0.0.0:8787", "", "attacker.test:8787"])
def test_dns_rebinding_host_header_is_refused_everywhere(host):
    app, cap, hand, _ = make()
    c = client(app)
    login(c, hand["launch_code"])                      # even a VALID session on a rebound host
    for method, path in (("GET", "/"), ("GET", "/api/projects"), ("POST", "/api/local/session"),
                         ("POST", "/api/local/handshake"), ("GET", "/assets/x.js")):
        r = c.request(method, path, headers={"Host": host}, json={} if method == "POST" else None)
        assert r.status_code == 400, (host, path, r.status_code)
        assert r.json()["error"]["code"] == "invalid_host"


@pytest.mark.parametrize("host", ["127.0.0.1:8787", "localhost:8787", "[::1]:8787", "LOCALHOST:8787"])
def test_loopback_hosts_are_accepted(host):
    app, *_ = make()
    assert client(app).get("/", headers={"Host": host}).status_code == 200


def _asgi(app, method, path, headers, body=b""):
    out = {}

    async def run():
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
        await app({"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                   "query_string": b"", "headers": headers, "scheme": "http", "http_version": "1.1",
                   "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 5), "root_path": ""},
                  receive, send)
    asyncio.run(run())
    return out["status"]


def test_missing_or_duplicate_host_header_is_refused():
    app, *_ = make()
    assert _asgi(app, "GET", "/", []) == 400
    good = (b"host", b"127.0.0.1:8787")
    assert _asgi(app, "GET", "/", [good, good]) == 400
    assert _asgi(app, "GET", "/", [good, (b"host", b"evil.example")]) == 400
    assert _asgi(app, "GET", "/", [good]) == 200


def test_origin_must_be_absent_or_same_origin_and_sec_fetch_site_is_honoured():
    app, cap, hand, _ = make()
    c = client(app)
    csrf = login(c, hand["launch_code"])
    ok = {"X-Agentic-CSRF": csrf}
    body = {"ctx": {}, "text": "hi"}
    assert c.post("/api/sidekick", json=body, headers=ok).status_code == 200                       # no Origin
    assert c.post("/api/sidekick", json=body, headers={**ok, "Origin": BASE_URL}).status_code == 200
    for origin in (EVIL, "null", "http://localhost:8787", "http://127.0.0.1:9999", "https://127.0.0.1:8787",
                   "http://127.0.0.1:8787.evil.example"):
        r = c.post("/api/sidekick", json=body, headers={**ok, "Origin": origin})
        assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed", origin
        # even a GET carrying a foreign Origin is refused
        assert c.get("/api/projects", headers={"Origin": origin}).status_code == 403
    for site in ("cross-site", "same-site"):
        assert c.post("/api/sidekick", json=body, headers={**ok, "Sec-Fetch-Site": site}).status_code == 403
        assert c.get("/api/projects", headers={"Sec-Fetch-Site": site}).status_code == 403
    assert c.post("/api/sidekick", json=body, headers={**ok, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.get("/api/projects", headers={"Sec-Fetch-Site": "none"}).status_code == 200   # typed URL
    assert c.post("/api/sidekick", json=body, headers={**ok, "Sec-Fetch-Site": "none"}).status_code == 403


def test_simple_request_csrf_form_post_without_custom_header_is_refused(secret_dir, walk_spy):
    app, cap, hand, _ = make()
    c = client(app)
    csrf = login(c, hand["launch_code"])               # the victim's browser holds a valid cookie
    for ctype, data in (("application/x-www-form-urlencoded", "project_id=x"),
                        ("text/plain", json.dumps(confirm_body(secret_dir))),
                        ("multipart/form-data; boundary=x", "--x--")):
        r = c.post("/api/sources/confirm", content=data, headers={"Content-Type": ctype})
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_invalid", ctype
    assert c.post("/api/sources/confirm", json=confirm_body(secret_dir),
                  headers={"X-Agentic-CSRF": csrf[:-1] + ("A" if csrf[-1] != "A" else "B")}).status_code == 403
    assert walk_spy == []
    # cross-origin form POST that also rides the cookie and (somehow) the token: Origin still refuses
    assert c.post("/api/sources/confirm", json=confirm_body(secret_dir),
                  headers={"X-Agentic-CSRF": csrf, "Origin": EVIL}).status_code == 403
    assert walk_spy == []


# --------------------------------------------------------------------------- launch code
def test_launch_code_missing_wrong_malformed_and_wrong_type_are_refused():
    app, cap, hand, _ = make(max_failures=1000)
    c = client(app)
    for body in ({}, {"code": ""}, {"code": "A" * 43}, {"code": 7}, {"code": None}, {"code": hand["launch_code"] + "x"},
                 {"code": hand["launch_code"][:-1]}, {"code": "../../etc"}, {"code": hand["launch_code"].lower()}):
        r = c.post("/api/local/session", json=body)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_launch_code", body
        assert "set-cookie" not in r.headers
    assert c.post("/api/local/session", content="not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/local/session", content="[]", headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/local/session", data={"code": hand["launch_code"]}).status_code == 415   # simple form
    assert c.post("/api/local/session", content=json.dumps({"code": hand["launch_code"]}),
                  headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/api/local/session", content=b"{" + b" " * 9000 + b"}",
                  headers={"Content-Type": "application/json"}).status_code == 400
    assert c.get("/api/local/session").status_code == 401        # nothing above created a session
    login(c, hand["launch_code"])                                # the real code survived all of that


def test_launch_code_is_single_use_and_replay_is_refused():
    app, cap, hand, _ = make()
    c1, c2 = client(app), client(app)
    login(c1, hand["launch_code"])
    r = c2.post("/api/local/session", json={"code": hand["launch_code"]})
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert c2.get("/api/projects").status_code == 401


def test_launch_code_expires():
    app, cap, hand, clock = make(launch_code_ttl=60)
    c = client(app)
    clock.t += 59.0
    code2 = cap.issue_launch_code()
    clock.t += 2.0                                       # first code is now 61 s old
    assert c.post("/api/local/session", json={"code": hand["launch_code"]}).status_code == 401
    login(c, code2)                                      # a later code is still valid


def test_failed_attempts_are_rate_limited_and_recover():
    app, cap, hand, clock = make(max_failures=5, failure_window=60.0)
    c = client(app)
    for _ in range(5):
        assert c.post("/api/local/session", json={"code": "wrong" * 5}).status_code == 401
    r = c.post("/api/local/session", json={"code": "wrong" * 5})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1     # guessing is throttled
    assert no_cors(r)
    clock.t += 61.0
    login(c, cap.issue_launch_code())                    # (the first code has expired meanwhile)


def test_sr04_a_correct_code_is_served_even_while_the_failure_budget_is_spent():
    """SR-04: 10 bad codes a minute from any local process used to lock sign-in out for good."""
    app, cap, hand, clock = make(max_failures=5, failure_window=60.0)
    c = client(app)
    for _ in range(5):
        assert c.post("/api/local/session", json={"code": "wrong" * 5}).status_code == 401
    assert c.post("/api/local/session", json={"code": "wrong" * 5}).status_code == 429
    csrf = login(c, hand["launch_code"])                 # the real, unexpired, unused code still works
    assert csrf and c.get("/api/projects").status_code == 200
    # ... and a REPLAY of it, or a wrong one, is still throttled
    assert client(app).post("/api/local/session", json={"code": hand["launch_code"]}).status_code == 429
    assert client(app).post("/api/local/session", json={"code": "wrong" * 5}).status_code == 429
    # an expired code is not served either (long window so the budget is still spent)
    app, cap, hand, clock = make(max_failures=2, failure_window=600.0)
    c = client(app)
    for _ in range(2):
        c.post("/api/local/session", json={"code": "wrong" * 5})
    clock.t += 61.0                                       # hand["launch_code"] (60 s) has expired
    assert c.post("/api/local/session", json={"code": hand["launch_code"]}).status_code == 429


def test_cross_origin_exchange_attempts_do_not_burn_the_rate_limit_or_the_code():
    app, cap, hand, _ = make(max_failures=3)
    c = client(app)
    for _ in range(10):
        assert c.post("/api/local/session", json={"code": "x" * 30}, headers={"Origin": EVIL}).status_code == 403
        assert c.post("/api/local/session", json={"code": hand["launch_code"]},
                      headers={"Origin": EVIL}).status_code == 403
    login(c, hand["launch_code"])                       # the legitimate exchange is unaffected


def test_concurrent_exchange_of_one_code_succeeds_exactly_once():
    app, cap, hand, _ = make()
    code = hand["launch_code"]
    with ThreadPoolExecutor(32) as ex:
        results = list(ex.map(lambda _: cap.consume_launch_code(code), range(64)))
    assert sum(results) == 1
    # and over HTTP, with separate clients racing on a fresh code
    code = cap.issue_launch_code()
    barrier = threading.Barrier(8)

    def attempt(_):
        c = client(app)
        barrier.wait()
        return c.post("/api/local/session", json={"code": code}).status_code
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(attempt, range(8)))
    assert sorted(codes).count(200) == 1 and all(x in (200, 401, 429) for x in codes)


# --------------------------------------------------------------------------- session
def test_session_is_a_header_credential_never_a_cookie_and_works_end_to_end():
    app, cap, hand, _ = make()
    c = client(app)
    r = c.post("/api/local/session", json={"code": hand["launch_code"]})
    assert r.status_code == 200 and "set-cookie" not in {k.lower() for k in r.headers}
    j = r.json()
    token, csrf = j["session_token"], j["csrf_token"]
    assert j["session_header"] == "X-Agentic-Session" and j["csrf_header"] == "X-Agentic-CSRF"
    assert len(b64url_decode(token)) == 32 and csrf != token and len(csrf) >= 40
    h = {"X-Agentic-Session": token}
    # no header: nothing works, even with a cookie of the same value under any name
    c2 = client(app)
    for name in ("agentic_os_session", "session", "X-Agentic-Session"):
        r = c2.get("/api/projects", headers={"Cookie": f"{name}={token}"})
        assert r.status_code == 401
    assert c2.get("/api/local/session").status_code == 401
    assert c2.get("/api/local/session", headers={"Cookie": f"agentic_os_session={token}"}).status_code == 401
    # a data call and a state-changing call, end to end
    assert c2.get("/api/projects", headers=h).json()[0]["id"] == "customer-ops"
    assert c2.post("/api/sidekick", json={"ctx": {}, "text": "hello"}, headers=h).status_code == 403   # no CSRF
    assert c2.post("/api/sidekick", json={"ctx": {}, "text": "hello"},
                   headers={**h, "X-Agentic-CSRF": csrf}).status_code == 200
    # reload of the same tab: the stored token re-yields the CSRF token; without the token nothing is handed out
    assert c2.get("/api/local/session", headers=h).json()["csrf_token"] == csrf
    # duplicate / garbled session headers are refused
    assert c2.get("/api/projects", headers=[("X-Agentic-Session", token), ("X-Agentic-Session", token)]).status_code == 401
    assert c2.get("/api/projects", headers={"X-Agentic-Session": token + "x"}).status_code == 401
    # logout needs the CSRF header and kills the session
    assert c2.delete("/api/local/session", headers=h).status_code == 403
    assert c2.delete("/api/local/session", headers={**h, "X-Agentic-CSRF": csrf}).status_code == 200
    assert c2.get("/api/projects", headers=h).status_code == 401


def test_session_is_bound_to_the_host_it_was_established_on():
    app, cap, hand, _ = make()
    c = client(app)
    token = c.post("/api/local/session", json={"code": hand["launch_code"]}).json()["session_token"]
    assert c.get("/api/projects", headers={"X-Agentic-Session": token}).status_code == 200
    assert c.get("/api/projects", headers={"X-Agentic-Session": token, "Host": "localhost:8787"}).status_code == 401
    assert c.get("/api/projects", headers={"X-Agentic-Session": token, "Host": "127.0.0.1:8787"}).status_code == 200


def test_sr02_a_hostile_local_server_on_another_port_receives_nothing_it_can_replay():
    """SR-02 regression. Cookies are not port-scoped: a browser sends a 127.0.0.1 cookie to EVERY local
    server. A hostile server on another port gets every cookie the browser holds for the host; it must be
    unable to use anything it receives. (Real-browser run of the reviewer's script: see the stage 3 report.)"""
    import http.server
    import httpx
    app, cap, hand, _ = make()
    victim = client(app)                                   # the victim's browser; its cookie jar is per host
    r = victim.post("/api/local/session", json={"code": hand["launch_code"]})
    assert r.status_code == 200
    seen: list = []

    class Hostile(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hostile)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # the victim navigates to the hostile page: the browser attaches every cookie it holds for 127.0.0.1
        # (the jar is shared with the sidecar's origin because cookies ignore the port)
        with httpx.Client(cookies=victim.cookies) as browser:
            browser.get(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()
    assert len(seen) == 1
    got = seen[0]
    assert "cookie" not in got and "x-agentic-session" not in got and "x-agentic-csrf" not in got
    # the attacker replays everything it received (plus guesses) against the sidecar: nothing works
    attacker = client(app)
    replay = {k: v for k, v in got.items() if k not in ("host", "connection", "accept-encoding", "user-agent")}
    for headers in (replay, {**replay, "Cookie": "agentic_os_session=" + "A" * 43}):
        assert attacker.get("/api/local/session", headers=headers).status_code == 401
        assert attacker.get("/api/projects", headers=headers).status_code == 401
        assert attacker.post("/api/sources/confirm", json={"sources": []}, headers=headers).status_code == 401


def test_session_expires():
    app, cap, hand, clock = make(session_ttl=100.0)
    c = client(app)
    login(c, hand["launch_code"])
    assert c.get("/api/projects").status_code == 200
    clock.t += 101
    assert c.get("/api/projects").status_code == 401


def test_sr08_expiry_stops_on_the_wall_clock_too_sleep_cannot_extend_a_lifetime():
    """time.monotonic does not advance while a Mac sleeps; the wall clock does. Dead when EITHER says so."""
    mono, wall = Clock(), Clock()
    wall.t = 1_700_000_000.0
    app, cap, hand, _ = make(clock=mono, wall_clock=wall, launch_code_ttl=60, session_ttl=3600)
    # launch code: the laptop sleeps 10 minutes (monotonic +1 s, wall +600 s)
    mono.t += 1.0
    wall.t += 600.0
    assert not cap.consume_launch_code(hand["launch_code"])
    # session
    code = cap.issue_launch_code()
    c = client(app)
    login(c, code)
    assert c.get("/api/projects").status_code == 200
    mono.t += 5.0
    wall.t += 7200.0                                      # sleep: wall +2 h, monotonic +5 s
    assert c.get("/api/projects").status_code == 401
    # challenge
    sn = c.post("/api/local/handshake", json={"client_nonce": "A" * 22}).json()["server_nonce"]
    wall.t += 31.0
    assert cap.mint_launch_code(sn, mint_proof(b64url_decode(hand["secret"]), PORT, sn)) is None


def test_sr08_backward_wall_clock_jumps_cannot_extend_and_forward_jumps_fail_closed():
    mono, wall = Clock(), Clock()
    wall.t = 1_700_000_000.0
    app, cap, hand, _ = make(clock=mono, wall_clock=wall, launch_code_ttl=60)
    c1 = cap.issue_launch_code()
    wall.t -= 10_000.0                                    # clock set back: the monotonic bound still holds
    mono.t += 59.0
    assert cap.consume_launch_code(c1)
    c2 = cap.issue_launch_code()
    wall.t -= 10_000.0
    mono.t += 61.0
    assert not cap.consume_launch_code(c2)                # not extended by the backward jump
    c3 = cap.issue_launch_code()
    wall.t += 10_000.0                                    # forward jump: fails closed
    assert not cap.consume_launch_code(c3)
    c4 = cap.issue_launch_code()
    mono.t += 59.0
    wall.t += 59.0
    assert cap.consume_launch_code(c4)                    # boundary: both clocks just inside
    assert cap.remaining((mono.t + 5, wall.t + 3)) == 3   # remaining() is the smaller of the two


# --------------------------------------------------------------------------- handshake / launcher proof
def test_handshake_proves_the_server_holds_the_secret_and_mint_proves_the_launcher_does():
    app, cap, hand, _ = make()
    c = client(app)
    secret = b64url_decode(hand["secret"])
    assert len(secret) == 32
    nonce = b64url(os.urandom(16))
    r = c.post("/api/local/handshake", json={"client_nonce": nonce}).json()
    assert r["proof"] == handshake_proof(secret, PORT, nonce)
    assert r["proof"] != handshake_proof(os.urandom(32), PORT, nonce)          # a foreign sidecar fails
    assert r["proof"] != handshake_proof(secret, PORT + 1, nonce)              # bound to the port
    # mint: needs the server's challenge AND the proof
    sn = r["server_nonce"]
    good = mint_proof(secret, PORT, sn)
    assert good != r["proof"]                                                  # domain separated
    bad = c.post("/api/local/launch-code", json={"server_nonce": sn}, headers={"X-Launcher-Proof": "A" * 43})
    assert bad.status_code == 401
    # the failed attempt burned the challenge: even the right proof is now refused (no online grinding)
    assert c.post("/api/local/launch-code", json={"server_nonce": sn},
                  headers={"X-Launcher-Proof": good}).status_code == 401
    sn2 = c.post("/api/local/handshake", json={"client_nonce": nonce}).json()["server_nonce"]
    ok = c.post("/api/local/launch-code", json={"server_nonce": sn2},
                headers={"X-Launcher-Proof": mint_proof(secret, PORT, sn2)})
    assert ok.status_code == 200 and ok.json()["expires_in"] == 60
    assert c.post("/api/local/launch-code", json={"server_nonce": sn2},                 # replay
                  headers={"X-Launcher-Proof": mint_proof(secret, PORT, sn2)}).status_code == 401
    login(c, ok.json()["launch_code"])
    # unknown / garbled nonce, short client nonce
    assert c.post("/api/local/launch-code", json={"server_nonce": "zz"},
                  headers={"X-Launcher-Proof": good}).status_code == 401
    assert c.post("/api/local/handshake", json={"client_nonce": "short"}).status_code == 400


def test_handshake_challenge_expires():
    app, cap, hand, clock = make()
    c = client(app)
    secret = b64url_decode(hand["secret"])
    sn = c.post("/api/local/handshake", json={"client_nonce": b64url(os.urandom(16))}).json()["server_nonce"]
    clock.t += 31
    assert c.post("/api/local/launch-code", json={"server_nonce": sn},
                  headers={"X-Launcher-Proof": mint_proof(secret, PORT, sn)}).status_code == 401


def test_proof_test_vector_for_the_rust_side():
    """Pinned vectors (also listed in docs/LOCAL_API_SECURITY.md) so the launcher can be tested against them."""
    secret = bytes(range(32))
    assert handshake_proof(secret, 8787, "AAAAAAAAAAAAAAAAAAAAAA") == VECTOR_HANDSHAKE
    assert mint_proof(secret, 8787, "BBBBBBBBBBBBBBBBBBBBBB") == VECTOR_MINT


VECTOR_HANDSHAKE = "XrELUoD7WHe1e8IgKZZYoltOOKdPcPN3PR-qWdhqCeY"   # key = bytes 0..31, computed independently of agentic_os
VECTOR_MINT = "JYxsI49qlI_AzpnZPgZwDLjyQLNRgueAarKpIxB19VQ"


def test_first_launch_code_is_optional_and_costs_no_slot_when_omitted():
    cap = LocalCapability(port=PORT)
    rec = cap.launcher_handoff("127.0.0.1", PORT, "", include_launch_code=False)
    assert "launch_code" not in rec and "launch_code_ttl" not in rec and len(b64url_decode(rec["secret"])) == 32
    assert cap._codes == []
    rec2 = cap.launcher_handoff("127.0.0.1", PORT, "")
    assert len(cap._codes) == 1 and rec2["launch_code_ttl"] == 60


def _mint(c, secret, nonce=None):
    nonce = nonce or b64url(os.urandom(16))
    sn = c.post("/api/local/handshake", json={"client_nonce": nonce}).json()["server_nonce"]
    return c.post("/api/local/launch-code", json={"server_nonce": sn},
                  headers={"X-Launcher-Proof": mint_proof(secret, PORT, sn)})


def test_a_legitimate_launcher_is_never_rate_limited_by_other_peoples_failures():
    app, cap, hand, clock = make(max_failures=3)
    c = client(app)
    secret = b64url_decode(hand["secret"])
    for _ in range(3):                                        # noise on the exchange endpoint: budget spent
        assert c.post("/api/local/session", json={"code": "x" * 30}).status_code == 401
    assert c.post("/api/local/session", json={"code": "x" * 30}).status_code == 429
    assert _mint(c, secret).status_code == 200                # separate budget: the launcher still mints
    for _ in range(3):                                        # noise on the mint endpoint itself
        r = c.post("/api/local/launch-code", json={"server_nonce": "z" * 22}, headers={"X-Launcher-Proof": "A" * 43})
        assert r.status_code == 401
    bad = c.post("/api/local/launch-code", json={"server_nonce": "z" * 22}, headers={"X-Launcher-Proof": "A" * 43})
    assert bad.status_code == 429 and int(bad.headers["retry-after"]) >= 1   # a wrong proof is throttled...
    ok = _mint(c, secret)                                     # ...a correct proof never is
    assert ok.status_code == 200 and len(ok.json()["launch_code"]) >= 40
    # the handshake endpoint is never rate limited (its state is capped instead)
    for _ in range(40):
        assert c.post("/api/local/handshake", json={"client_nonce": "A" * 22}).status_code == 200
    # a verified handshake whose challenge was evicted/expired fails with 401 (not 429): retry handshake + mint
    sn = c.post("/api/local/handshake", json={"client_nonce": "B" * 22}).json()["server_nonce"]
    clock.t += 31
    r = c.post("/api/local/launch-code", json={"server_nonce": sn},
               headers={"X-Launcher-Proof": mint_proof(secret, PORT, sn)})
    assert r.status_code in (401, 429)
    clock.t += 61
    assert _mint(c, secret).status_code == 200


def test_secret_is_generated_in_memory_never_accepted_and_never_in_repr():
    a, b = LocalCapability(), LocalCapability()
    assert a.launcher_handoff("127.0.0.1", 1)["secret"] != b.launcher_handoff("127.0.0.1", 1)["secret"]
    with pytest.raises(TypeError):
        LocalCapability(secret="chosen-by-the-web")          # type: ignore[call-arg]
    rec = a.launcher_handoff("127.0.0.1", 8787, "")
    line = json.dumps(rec, separators=(",", ":"))
    assert "\n" not in line and line.isascii()
    assert len(b64url_decode(rec["secret"])) >= 32 and len(b64url_decode(rec["launch_code"])) >= 32
    for text in (repr(a), str(a.__dict__.keys())):
        assert rec["secret"] not in text and rec["launch_code"] not in text


def test_comparisons_use_constant_time_primitives(monkeypatch):
    calls = []
    real = _hmac.compare_digest
    monkeypatch.setattr(lc.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    app, cap, hand, _ = make()
    c = client(app)
    login(c, hand["launch_code"])                        # launch code compare + session lookup compare
    n = len(calls)
    assert n >= 1
    c.get("/api/projects")
    assert len(calls) > n                                # session token compare
    csrf = c.get("/api/local/session").json()["csrf_token"]
    before = len(calls)
    c.post("/api/sidekick", json={"ctx": {}, "text": "x"}, headers={"X-Agentic-CSRF": csrf})
    assert len(calls) >= before + 2                      # session + CSRF
    sn = c.post("/api/local/handshake", json={"client_nonce": "A" * 22}).json()["server_nonce"]
    before = len(calls)
    c.post("/api/local/launch-code", json={"server_nonce": sn}, headers={"X-Launcher-Proof": "A" * 43})
    assert len(calls) >= before + 2                      # challenge lookup + proof
    src = Path(lc.__file__).read_text()
    assert not re.search(r"(secret|proof|csrf|token|code)\w*\s*(==|!=)\s", src.replace("code ==", ""))


# --------------------------------------------------------------------------- headers
def test_security_headers_and_no_store():
    app, cap, hand, _ = make()
    c = client(app)
    login(c, hand["launch_code"])
    for r in (c.get("/"), c.get("/api/projects"), c.get("/nope"), c.get("/api/projects", headers={"Origin": EVIL}),
              c.post("/api/local/session", json={"code": "x" * 30})):
        h = {k.lower(): v for k, v in r.headers.items()}
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert h["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "default-src 'none'" in h["content-security-policy"]
        assert "script-src 'self'" in h["content-security-policy"] and "unsafe-eval" not in h["content-security-policy"]
    assert c.get("/api/projects").headers["cache-control"] == "no-store"
    assert c.get("/nope").headers["cache-control"] == "no-store"
    assert c.post("/api/local/session", json={"code": "x" * 30}).headers["cache-control"] == "no-store"


def test_vendored_ui_is_compatible_with_the_csp_static_check():
    """STATIC: the CSP is script-src 'self'. The bundle must not need eval / inline script / inline handlers."""
    ui = ROOT / "agentic_os" / "projects_ui"
    app, *_ = make()
    html = client(app).get("/").text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)                # no inline script
    assert not re.search(r"\son[a-z]+\s*=", html)                              # no inline handlers
    assert not re.search(r"<script[^>]+src=\"https?://", html)                 # nothing cross-origin
    js = "\n".join(p.read_text(errors="ignore") for p in (ui / "assets").glob("*.js"))
    assert not re.search(r"\beval\(|new Function\(|setTimeout\(\s*[\"']|setInterval\(\s*[\"']", js)
    css = "\n".join(p.read_text(errors="ignore") for p in (ui / "assets").glob("*.css"))
    assert not re.search(r"@import|url\(\s*[\"']?https?:", css)


# --------------------------------------------------------------------------- the UI shim (node vm)
_SHIM_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const mode = process.argv[3];
const log = [];
const store = {};
if (mode === 'reload' || mode === 'neterr' || mode === 'e500') store['agentic_os.session'] = 'STORED-TOKEN';
const location = { origin: 'http://127.0.0.1:8787', href: 'http://127.0.0.1:8787/',
  pathname: '/', search: '', hash: mode === 'code' ? '#lc=' + 'A'.repeat(43) : '' };
const history = { replaceState(_a, _b, url) { log.push(['replaceState', url]); location.hash = ''; } };
class Headers { constructor(h){ this.m = {}; if (h instanceof Headers) Object.assign(this.m, h.m); else if (h) for (const k in h) this.m[k.toLowerCase()] = h[k]; }
  set(k,v){ this.m[k.toLowerCase()] = v; } get(k){ return this.m[k.toLowerCase()]; } }
const hdrs = (o) => o && o.headers && (o.headers instanceof Headers ? o.headers.m : o.headers);
const fetchImpl = (u, o) => { log.push(['fetch', String(u), (o && o.method) || 'GET', o && o.credentials, hdrs(o) || null, (o && o.body) || null, location.hash]);
  if (mode === 'neterr' && !String(u).endsWith('/api/local/session') && !String(u).includes('/api/sidekick')) return Promise.reject(new TypeError('Failed to fetch'));
  if (mode === 'e500' && String(u).endsWith('/api/projects')) return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) });
  const ok = String(u).endsWith('/api/local/session') && (mode !== 'none');
  const status = ok ? 200 : (String(u).endsWith('/api/local/session') ? 401 : 200);
  return Promise.resolve({ ok: status === 200, status, json: () => Promise.resolve({ session_token: 'SESSIONTOKEN', csrf_token: 'CSRFTOKEN' }) }); };
const sessionStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } };
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this._text = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { this.children.push(c); return c; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return [this._text].concat(this.children.map((c) => c.textContent)).filter(Boolean).join(' '); }
  focus() { document.activeElement = this; }
  remove() { const i = document.body.children.indexOf(this); if (i >= 0) document.body.children.splice(i, 1); }
  toJSON() { return { tag: this.tag, attrs: this.attrs, text: this._text, children: this.children }; }
}
const root = new El('div'); root.attrs.id = 'root';
const document = { currentScript: { src: 'http://127.0.0.1:8787/local-capability-shim.js' }, activeElement: null,
  body: new El('body'), head: new El('head'), addEventListener() {},
  getElementById(id) { return id === 'root' ? root : null; }, createElement(t) { return new El(t); } };
const window = { fetch: fetchImpl, location, history, sessionStorage };
const ctx = { window, document, location, history, URL, Headers, Object, JSON, Promise };
vm.createContext(ctx);
vm.runInContext(src.replace('window.fetch.bind(window)', 'window.fetch'), ctx);
(async () => {
  const snaps = [];
  const call = async (...a) => { try { await ctx.window.fetch(...a); } catch (e) { log.push(['rejected', String(e)]); }
    snaps.push({ n: document.body.children.length, texts: document.body.children.map((e) => e.textContent), roles: document.body.children.map((e) => e.attrs.role || ''),
      root: Object.assign({}, root.attrs), focus: document.activeElement && document.activeElement.tag }); };
  await call('/api/projects');
  await call('/api/sidekick', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  await call('https://example.org/api/x');
  const panels = document.body.children;
  console.log(JSON.stringify({ log, store, snaps, banner: panels.length, texts: panels.map((e) => e.textContent), panels,
    root: root.attrs, styles: document.head.children.length, focus: document.activeElement && document.activeElement.tag }));
})();
"""


def _run_shim(tmp_path, mode):
    harness = tmp_path / "harness.js"
    harness.write_text(_SHIM_HARNESS)
    out = subprocess.run(["node", str(harness), str(ROOT / "agentic_os" / "local_capability_shim.js"), mode],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    for entry in res["log"]:                              # header names are case-insensitive
        if entry[0] == "fetch" and entry[4]:
            entry[4] = {k.lower(): v for k, v in entry[4].items()}
    return res


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shim_exchanges_fragment_code_keeps_the_token_in_session_storage_and_sends_headers(tmp_path):
    r = _run_shim(tmp_path, "code")
    log = r["log"]
    assert log[0] == ["replaceState", "/"]                                         # fragment removed first
    ex = log[1]
    assert ex[1].endswith("/api/local/session") and ex[2] == "POST" and json.loads(ex[5]) == {"code": "A" * 43}
    assert ex[6] == "" and ex[3] == "omit"                                         # hash already cleared; no cookies
    assert r["store"] == {"agentic_os.session": "SESSIONTOKEN"}                    # per-tab storage, not a cookie
    api = log[2]
    assert api[4]["x-agentic-session"] == "SESSIONTOKEN" and api[4]["x-agentic-csrf"] == "CSRFTOKEN"
    assert api[3] == "omit"
    post = log[3]
    assert post[4]["x-agentic-session"] == "SESSIONTOKEN" and post[4]["x-agentic-csrf"] == "CSRFTOKEN"
    assert post[4]["content-type"] == "application/json"
    assert log[4][4] is None                                                       # cross-origin: untouched
    assert r["banner"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shim_resumes_a_reloaded_tab_from_session_storage_and_a_new_tab_needs_a_code(tmp_path):
    r = _run_shim(tmp_path, "reload")                    # same tab, hash empty, token in sessionStorage
    first = r["log"][0]
    assert first[1].endswith("/api/local/session") and first[2] == "GET"
    assert first[4]["x-agentic-session"] == "STORED-TOKEN"
    assert r["log"][1][4]["x-agentic-session"] == "STORED-TOKEN" and r["log"][1][4]["x-agentic-csrf"] == "CSRFTOKEN"
    r = _run_shim(tmp_path, "none")                      # new tab / bookmark: no code, no stored token
    assert [e for e in r["log"] if e[1].endswith("/api/local/session")] == []      # nothing to resume with
    assert r["banner"] == 1                                                        # tells the user what to do
    assert "x-agentic-session" not in (r["log"][0][4] or {})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shim_signed_out_panel_tells_a_user_what_to_press_and_what_does_not_sign_in(tmp_path):
    """Text (R0-prime decision 5, U-02): the sign-in panel names the launcher button and the three things that
    cannot sign in on their own; the developer sentence is behind a <details>; the window is called
    "Agentic Apps R0-prime"."""
    r = _run_shim(tmp_path, "none")
    assert len(r["texts"]) == 1
    text = r["texts"][0]
    assert "Not signed in to the local Agentic Apps service" in text
    assert 'press "Start & open"' in text                                          # the launcher button (deploy/launcher/ui/index.html)
    for word in ("bookmark", "restored tab", "second tab", "works once", "one-time URL"):
        assert word in text, word
    assert "Agentic Apps R0-prime window" in text and "ReDevOps" not in text
    assert "#lc=" not in text and "secret" not in text.lower()                      # never mentions a credential value
    ui = (ROOT / "deploy" / "launcher" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "Start &amp; open" in ui                                                # the button the panel names exists


def _walk(node):
    yield node
    for c in node["children"]:
        yield from _walk(c)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shim_signed_out_panel_structure_replaces_the_dead_shell_and_takes_focus(tmp_path):
    """U-02: ONE <main> with an <h1> that takes focus, a role=alert message, the developer sentence in a closed
    <details>, the About hook as a relative link, and the dead UI shell (#root) hidden + inert. No fixed strip."""
    r = _run_shim(tmp_path, "none")
    assert r["banner"] == 1 and r["root"].get("hidden") == "" and r["root"].get("inert") == ""
    assert r["root"].get("aria-hidden") == "true"
    assert r["focus"] == "h1"
    panel = r["panels"][0]
    nodes = list(_walk(panel))
    assert panel["tag"] == "main" and panel["attrs"]["id"] == "agentic-signin"
    assert [n["text"] for n in nodes if n["tag"] == "h1"] == ["Sign in from the Agentic Apps R0-prime window"]
    alerts = [n for n in nodes if n["attrs"].get("role") == "alert"]
    assert len(alerts) == 1 and "Not signed in to the local Agentic Apps service" in " ".join(
        c["text"] for c in alerts[0]["children"])
    details = [n for n in nodes if n["tag"] == "details"]
    assert len(details) == 1 and "open" not in details[0]["attrs"]
    dev_text = " ".join(n["text"] for n in _walk(details[0]))
    assert "serve command" in dev_text and "container" in dev_text and "one-time URL" in dev_text
    assert not any("serve command" in n["text"] for n in nodes if n not in list(_walk(details[0])))
    links = [n for n in nodes if n["tag"] == "a"]
    assert [(n["attrs"]["href"], n["text"]) for n in links] == [("./about/", "About and licences")]
    assert r["styles"] == 1
    css = [c for c in nodes if c["tag"] == "main"]
    assert css                                                                       # (style lives in <head>)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shim_signed_in_paths_do_not_show_the_panel(tmp_path):
    for mode in ("code", "reload"):
        r = _run_shim(tmp_path, mode)
        assert r["banner"] == 0 and r["root"] == {"id": "root"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("mode,expect", [("neterr", "not responding"), ("e500", "HTTP 500")])
def test_shim_tells_the_user_when_the_local_service_is_gone_or_failing_and_clears_it_on_recovery(tmp_path, mode, expect):
    """UI-agent finding (rel/uiagent-macos, release qualification 2026-11-05): when the sidecar is stopped or fails, the vendored
    bundle shows "Loading..." forever with no message (it has no error state). The shim, which already wraps every same-origin
    /api/ fetch, shows ONE non-blocking role=alert notice naming the launcher button, never moves focus, never hides the app, and
    removes the notice when a later same-origin /api/ request gets any HTTP answer below 500. Cross-origin requests are ignored."""
    r = _run_shim(tmp_path, mode)
    s1, s2, s3 = r["snaps"]
    assert s1["n"] == 1 and s1["roles"] == ["alert"], s1                           # the first /api/projects failure shows the notice
    text = s1["texts"][0]
    assert expect in text, text
    assert 'press "Start & open"' in text and "Agentic Apps R0-prime window" in text and "ReDevOps" not in text
    assert "#lc=" not in text and "secret" not in text.lower() and "token" not in text.lower()
    assert s1["focus"] is None and s1["root"] == {"id": "root"}                    # non-blocking: focus stays, the app is not hidden/inert
    assert s2["n"] == 0, s2                                                       # a later POST /api/sidekick that is answered clears it
    assert s3["n"] == 0, s3                                                       # a failing CROSS-ORIGIN request never raises the notice
    assert not [e for e in r["log"] if e[0] == "replaceState"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shim_service_notice_does_not_replace_or_double_the_signed_out_panel(tmp_path):
    r = _run_shim(tmp_path, "none")                                                # signed out: the sign-in panel owns the page
    assert [s["n"] for s in r["snaps"]] == [1, 1, 1]                                # one panel, never a second element
    assert r["texts"][0].startswith("Sign in from the Agentic Apps R0-prime window")


def test_shim_keeps_the_csp_as_strict_as_before():
    """The panel is built with DOM APIs and one <style> element: no inline script, no eval, no inline event
    handler attributes and no new network request in the shim; the CSP string is unchanged."""
    js = (ROOT / "agentic_os" / "local_capability_shim.js").read_text(encoding="utf-8")
    assert not re.search(r"\beval\(|new Function\(|innerHTML|insertAdjacentHTML|document\.write|setTimeout\(\s*[\"']", js)
    assert not re.search(r"\bon[a-z]+\s*=", js.replace("onerror", ""))
    assert js.count("origFetch(") == 4                                           # unchanged: exchange, resume, wrapper pass-through x2
    assert "XMLHttpRequest" not in js and "sendBeacon" not in js and "new WebSocket" not in js
    app, *_ = make()
    assert client(app).get("/").headers["content-security-policy"] == (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'")


# --------------------------------------------------------------------------- real processes
class _free_port_server:
    """Context manager: `serve` on a free fixed port; yields (port, proc). Kills it on exit."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path

    def __enter__(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env = _clean_env(PROJECTS_PORT=str(port))
        self.proc = subprocess.Popen([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                                     env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("serve exited: " + self.proc.stderr.read().decode()[-500:])
                time.sleep(0.2)
        else:
            self.proc.kill()
            raise RuntimeError("serve did not start")
        return port, self.proc

    def __exit__(self, *a):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for f in (self.proc.stdout, self.proc.stderr):
            f.close()


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("APPROVAL_SECRET", "AGENT_GATEWAY_TOKEN", "SSH_AUTH_SOCK", "SSH_AGENT_PID")
           and not k.startswith(("PROJECTS_", "AGENTIC_OS_"))}
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def _http(method, url, *, body=None, headers=None, cookie=None):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode(), {k.title(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), {k.title(): v for k, v in e.headers.items()}


def _read_until(proc_stream, pattern, timeout=30):
    out, deadline = [], time.time() + timeout
    box = {}

    def reader():
        for raw in iter(proc_stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            out.append(line)
            m = re.search(pattern, line)
            if m:
                box["m"] = m
                return
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(max(0.1, deadline - time.time()))
    return box.get("m"), out


def test_standalone_serve_prints_a_one_time_ascii_url_on_stderr_and_it_works():
    env = _clean_env(PROJECTS_PORT="0")
    p = subprocess.Popen([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                         env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        m, lines = _read_until(p.stderr, r"http://127\.0\.0\.1:(\d+)/#lc=([A-Za-z0-9_-]+)")
        assert m, lines
        port, code = int(m.group(1)), m.group(2)
        assert port != 0 and all(line.isascii() for line in lines)
        assert len(b64url_decode(code)) >= 32
        base = f"http://127.0.0.1:{port}"
        # not guessable by a web page: no credential -> 401; wrong code -> 401
        assert _http("GET", base + "/api/projects")[0] == 401
        assert _http("POST", base + "/api/local/session", body={"code": "A" * 43})[0] == 401
        st, body, hdrs = _http("POST", base + "/api/local/session", body={"code": code})
        assert st == 200
        assert "Set-Cookie" not in hdrs
        sess = {"X-Agentic-Session": json.loads(body)["session_token"]}
        csrf = json.loads(body)["csrf_token"]
        assert _http("GET", base + "/api/projects", headers=sess)[0] == 200
        assert _http("GET", base + "/api/local/session")[0] == 401
        assert _http("POST", base + "/api/sidekick", body={"ctx": {}, "text": "hi"}, headers=sess)[0] == 403
        assert _http("POST", base + "/api/sidekick", body={"ctx": {}, "text": "hi"},
                     headers={**sess, "X-Agentic-CSRF": csrf})[0] == 200
        assert _http("POST", base + "/api/local/session", body={"code": code})[0] == 401     # single use
        assert _http("GET", base + "/", headers={"Host": "evil.example"})[0] == 400
        # the secret is never emitted in standalone mode, and the code is not on stdout
        p.terminate()
        out = p.stdout.read().decode("utf-8", "replace")
        assert code not in out
    finally:
        p.kill()
        p.wait()
        p.stdout.close()
        p.stderr.close()


def test_launcher_handoff_over_stdout_and_stale_sidecar_cannot_be_adopted():
    env = _clean_env(PROJECTS_PORT="0", AGENTIC_OS_HANDOFF="stdout")
    p = subprocess.Popen([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                         env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        raw = p.stdout.readline()
        assert raw.endswith(b"\n") and not raw.endswith(b"\r\n") and raw.count(b"\n") == 1
        rec = json.loads(raw.decode())
        assert rec["event"] == "ready" and rec["proto"] == lc.PROTO and rec["v"] == 1
        assert rec["host"] == "127.0.0.1" and rec["port"] > 0 and rec["pid"] == p.pid
        secret = b64url_decode(rec["secret"])
        assert len(secret) == 32
        assert "launch_code" not in rec and "launch_code_ttl" not in rec     # optional; omitted in launcher mode
        base = f"http://127.0.0.1:{rec['port']}"
        # handshake: the launcher verifies the server's proof; a stale/foreign process cannot produce it
        nonce = b64url(os.urandom(16))
        st, body, _ = _http("POST", base + "/api/local/handshake", body={"client_nonce": nonce})
        assert st == 200 and json.loads(body)["proof"] == handshake_proof(secret, rec["port"], nonce)
        assert json.loads(body)["proof"] != handshake_proof(os.urandom(32), rec["port"], nonce)
        sn = json.loads(body)["server_nonce"]
        st, body, _ = _http("POST", base + "/api/local/launch-code", body={"server_nonce": sn},
                            headers={"X-Launcher-Proof": mint_proof(secret, rec["port"], sn)})
        assert st == 200
        code = json.loads(body)["launch_code"]
        st, body, hdrs = _http("POST", base + "/api/local/session", body={"code": code})
        assert st == 200
        assert "Set-Cookie" not in hdrs
        sess = {"X-Agentic-Session": json.loads(body)["session_token"]}
        assert _http("GET", base + "/api/projects", headers=sess)[0] == 200
        p.terminate()
        rest, err = p.stdout.read().decode(), p.stderr.read().decode()
        for stream in (rest, err):                          # secret/code never in logs, banner or access log
            assert rec["secret"] not in stream and code not in stream
            assert "#lc=" not in stream
    finally:
        p.kill()
        p.wait()
        p.stdout.close()
        p.stderr.close()


def test_launcher_handoff_can_still_carry_a_first_launch_code_when_asked():
    env = _clean_env(PROJECTS_PORT="0", AGENTIC_OS_HANDOFF="stdout", AGENTIC_OS_HANDOFF_LAUNCH_CODE="1")
    p = subprocess.Popen([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                         env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        rec = json.loads(p.stdout.readline().decode())
        assert rec["launch_code_ttl"] == 60 and len(b64url_decode(rec["launch_code"])) == 32
        st, _, _ = _http("POST", f"http://127.0.0.1:{rec['port']}/api/local/session", body={"code": rec["launch_code"]})
        assert st == 200
    finally:
        p.kill()
        p.wait()
        p.stdout.close()
        p.stderr.close()


def test_handoff_line_is_written_as_bytes_with_a_single_lf_even_on_a_translating_stream(monkeypatch):
    """A Windows text-mode stdout turns "\\n" into CR LF; the hand-off must bypass that translation."""
    import io
    from agentic_os.local_serve import _handoff_writer
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="ascii", newline="\r\n")           # translating, like Windows
    monkeypatch.setattr(sys, "stdout", fake)
    write, close = _handoff_writer("stdout")
    write('{"v":1,"event":"ready"}')
    fake.flush()
    assert raw.getvalue() == b'{"v":1,"event":"ready"}\n'
    assert b"\r" not in raw.getvalue()
    # control: a plain text write on the same stream WOULD have produced CR LF
    fake.write("x\n")
    fake.flush()
    assert raw.getvalue().endswith(b"x\r\n")


def test_launcher_handoff_over_inherited_fd():
    r, w = os.pipe()
    env = _clean_env(PROJECTS_PORT="0", AGENTIC_OS_HANDOFF=f"fd:{w}")
    p = subprocess.Popen([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                         env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(w,))
    os.close(w)
    try:
        with os.fdopen(r, "rb") as f:
            line = f.readline()
        assert line.endswith(b"\n") and not line.endswith(b"\r\n") and line.count(b"\n") == 1
        rec = json.loads(line.decode())
        assert rec["event"] == "ready" and rec["port"] > 0 and len(b64url_decode(rec["secret"])) == 32
        assert _http("GET", f"http://127.0.0.1:{rec['port']}/api/projects")[0] == 401
    finally:
        p.terminate()
        out, err = p.communicate(timeout=15)
    assert rec["secret"].encode() not in out + err


def test_launcher_mode_reports_bind_conflict_without_a_secret_and_refuses_non_loopback():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        p = subprocess.run([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                           env=_clean_env(PROJECTS_PORT=str(port), AGENTIC_OS_HANDOFF="stdout"),
                           cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        rec = json.loads(p.stdout.strip().splitlines()[0])
        assert p.returncode == 1 and rec["event"] == "error" and rec["code"] == "bind_failed" and "secret" not in rec
    finally:
        s.close()
    for extra in ({"PROJECTS_HOST": "0.0.0.0"}, {"AGENTIC_OS_LOCAL_CAPABILITY": "off"}):
        p = subprocess.run([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                           env=_clean_env(PROJECTS_PORT="0", AGENTIC_OS_HANDOFF="stdout", **extra),
                           cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        assert p.returncode == 2 and p.stdout == "" and "refusing to start" in p.stderr


def test_sidecar_main_serve_path_goes_through_the_gate():
    """packaging/sidecar_main.py (the frozen entry) calls projects_api.main(): same gate."""
    src = (ROOT / "packaging" / "sidecar_main.py").read_text()
    assert "from agentic_os.projects_api import main as serve" in src
    api = (ROOT / "agentic_os" / "projects_api.py").read_text()
    assert "from .local_serve import serve" in api


# --------------------------------------------------------------------------- SR-06: stalled clients
def test_sr06_a_stalled_request_body_is_cut_off_instead_of_pinning_the_connection():
    """A client that sends headers and then never finishes the body must not hold the request forever."""
    cap = LocalCapability(port=PORT)
    inner = create_app(SampleProjectionProvider())
    gate = lc.LocalCapabilityMiddleware(inner, cap, body_timeout=0.3)
    token, sess = cap.new_session("127.0.0.1:8787")
    got = {}

    async def run():
        first = {"sent": False}

        async def receive():
            if not first["sent"]:
                first["sent"] = True
                return {"type": "http.request", "body": b'{"ctx": ', "more_body": True}
            await asyncio.sleep(3600)                       # the client goes silent

        async def send(m):
            if m["type"] == "http.response.start":
                got["status"] = m["status"]
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(gate({
                "type": "http", "method": "POST", "path": "/api/sidekick", "raw_path": b"/api/sidekick",
                "query_string": b"", "scheme": "http", "http_version": "1.1", "root_path": "",
                "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 5),
                "headers": [(b"host", b"127.0.0.1:8787"), (b"x-agentic-session", token.encode()),
                            (b"x-agentic-csrf", sess.csrf.encode()), (b"content-type", b"application/json"),
                            (b"content-length", b"1000")]}, receive, send), 8)
        except Exception:
            pass                                             # the app may fail on the cut body; it must END
        got["elapsed"] = time.monotonic() - t0
    asyncio.run(run())
    assert got["elapsed"] < 6, got                           # ended by the body timeout, not by the 8 s guard

    # the same for the launch-code endpoint's own body reader
    async def run2():
        async def receive():
            await asyncio.sleep(3600)

        async def send(m):
            if m["type"] == "http.response.start":
                got["local"] = m["status"]
        await asyncio.wait_for(gate({
            "type": "http", "method": "POST", "path": "/api/local/session", "raw_path": b"/api/local/session",
            "query_string": b"", "scheme": "http", "http_version": "1.1", "root_path": "",
            "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 5),
            "headers": [(b"host", b"127.0.0.1:8787"), (b"content-type", b"application/json")]}, receive, send), 5)
    asyncio.run(run2())
    assert got["local"] == 400


def _spawn_serve(env_extra=None, preexec=None):
    env = _clean_env(PROJECTS_PORT="0", **(env_extra or {}))
    p = subprocess.Popen([sys.executable, "-c", "from agentic_os.projects_api import main; main()"],
                         env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=preexec)
    m, lines = _read_until(p.stderr, r"http://127\.0\.0\.1:(\d+)/#lc=")
    assert m, lines
    return p, int(m.group(1))


def test_sr06_graceful_exit_is_not_blocked_by_a_stalled_connection():
    p, port = _spawn_serve()
    socks = []
    try:
        for path in ("/api/local/handshake", "/api/local/session"):     # the reviewer's SR-06 stall: 10 of 1000 body bytes
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall((f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n0123456789").encode())
            socks.append(s)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)       # header-stalled too
        s.sendall(b"GET /api/projects HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Slow: " % port)
        socks.append(s)
        time.sleep(0.5)
        t0 = time.monotonic()
        p.terminate()                                        # SIGTERM: uvicorn's graceful path
        rc = p.wait(timeout=20)
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"exit took {elapsed:.1f}s with stalled clients (launcher grace is 3 s)"
    finally:
        for s in socks:
            s.close()
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()
        p.stderr.close()


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_NOFILE is POSIX only")
def test_sr06_many_stalled_connections_do_not_exhaust_a_256_fd_sidecar():
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < 1024:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(1024, hard) if hard != resource.RLIM_INFINITY else 1024, hard))
        except (ValueError, OSError):
            pytest.skip("cannot raise the test process's own descriptor limit")

    def small_limit():
        h = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, h))
    p, port = _spawn_serve(preexec=small_limit)
    socks = []
    try:
        # The listen backlog is 128 and the sidecar's single event loop accepts in batches, so an
        # unpaced burst of 320 connects can see resets/timeouts from the OS (macOS drops the overflow):
        # a property of the test client, not of the sidecar. Pace the connects in batches, retry a
        # connection the OS refused (bounded), and require that (nearly) all 320 stalled clients exist.
        def stalled_connection():
            last = None
            for _ in range(8):
                try:
                    s = socket.create_connection(("127.0.0.1", port), timeout=5)
                    s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Slow: " % port)   # headers never finish
                    return s
                except OSError as e:
                    last = e
                    time.sleep(0.1)
            raise AssertionError(f"could not establish a stalled client after 8 tries: {last!r}")

        for i in range(320):
            socks.append(stalled_connection())
            if i % 32 == 31:
                time.sleep(0.05)                 # let the server's loop accept the batch
        assert len(socks) == 320
        # the launcher's handshake must work with the stalled clients present; allow the OS/loop a bounded
        # moment (5 s) instead of a single attempt
        deadline = time.monotonic() + 5.0
        while True:
            nonce = b64url(os.urandom(16))
            try:
                st, body, _ = _http("POST", f"http://127.0.0.1:{port}/api/local/handshake", body={"client_nonce": nonce})
            except OSError:
                st = None
            if st == 200 or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        assert st == 200, "the launcher's handshake must still work with 320 stalled clients"
    finally:
        for s in socks:
            s.close()
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stdout.close()
        p.stderr.close()
