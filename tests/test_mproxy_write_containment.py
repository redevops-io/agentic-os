"""PR-B: writes through the module proxy are refused on a non-loopback bind (issue #528).

Two-sided, per the owner clarification: the documented public demo READS must keep working anonymously on
every bind, while an anonymous WRITE must be refused AND must produce no effect at the module behind the
proxy. Every refusal here is asserted against a real upstream server that records what it was asked to do,
so "refused" is proven as "the module never saw it", not as a status code alone.

The bind under test is the client's own socket address (TestClient's base_url), which is the same value
uvicorn puts in the ASGI ``server`` scope in production. No test sends an edge header to argue its way in,
and one test tries to.
"""
from __future__ import annotations

import http.server
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEMO_READ_ONLY", raising=False)
    monkeypatch.delenv("DEMO_ALLOW_WRITES", raising=False)
    from agentic_os import control_plane
    return control_plane


@pytest.fixture()
def upstream(cp_mod, monkeypatch):
    """A stand-in module service that records every request it receives."""
    seen: list[tuple[str, str]] = []

    class H(http.server.BaseHTTPRequestHandler):
        def _reply(self):
            # an http.server handler that ignores a POST body leaves the connection in a bad state and the
            # proxy reports it as an unreachable upstream, so the body has to be read before answering.
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            seen.append((self.command, self.path))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        do_GET = do_POST = _reply

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    name = next(iter(cp_mod.MODULE_SERVICES))
    monkeypatch.setitem(cp_mod.MODULE_SERVICES, name, f"http://127.0.0.1:{srv.server_port}")
    try:
        yield name, seen
    finally:
        srv.shutdown()


LOOPBACK = "http://127.0.0.1:8080"
# The shipped compose binds 0.0.0.0 (deploy/docker-compose.yml:29); an interface address is the other way
# a real host answers beyond the machine. The IPv6 wildcard ("::") is covered in the classification test
# below: Starlette's TestClient parses its base_url by splitting on ":", so it cannot express an IPv6
# host — the scope value it would produce is what matters, and that is asserted directly.
PUBLIC_BINDS = ["http://0.0.0.0:8080", "http://10.0.0.7:8080"]


@pytest.mark.parametrize("bind", [LOOPBACK, *PUBLIC_BINDS])
def test_anonymous_reads_stay_open_on_every_bind(cp_mod, upstream, bind):
    name, seen = upstream
    c = TestClient(cp_mod.app, base_url=bind)          # no credentials, and no edge header pretending
    assert c.get(f"/m/{name}/api/status").status_code == 200, bind
    assert c.get(f"/m/{name}").status_code == 200, bind          # the proxied dashboard itself
    assert ("GET", "/api/status") in seen, "a documented public read must still reach the module"


@pytest.mark.parametrize("bind", PUBLIC_BINDS)
def test_anonymous_post_is_refused_on_a_public_bind_and_reaches_nobody(cp_mod, upstream, bind):
    name, seen = upstream
    r = TestClient(cp_mod.app, base_url=bind).post(f"/m/{name}/api/agent/chat", content=b'{"text":"go"}')
    assert r.status_code == 403, bind
    assert r.json()["write_refused"] is True
    assert [p for p in seen if p[0] == "POST"] == [], "a refused write must not touch the module"


def test_the_same_write_still_works_locally(cp_mod, upstream):
    """Local dev is unchanged: a loopback bind forwards, which is why the refusal is a property of the
    deployment and not a flag someone has to remember to set."""
    name, seen = upstream
    r = TestClient(cp_mod.app, LOOPBACK).post(f"/m/{name}/api/agent/chat", content=b'{"text":"hi"}')
    assert r.status_code == 200, r.text
    assert ("POST", "/api/agent/chat") in seen


@pytest.mark.parametrize("spoof", [
    {"cf-connecting-ip": "127.0.0.1"},
    {"x-forwarded-for": "127.0.0.1"},
    {"host": "localhost"},
    {"cf-connecting-ip": "127.0.0.1", "x-forwarded-for": "127.0.0.1", "host": "localhost:8080"},
])
def test_no_header_can_claim_the_request_came_from_loopback(cp_mod, upstream, spoof):
    """The gate reads the socket, not the headers — so bypassing Cloudflare, or pretending to, changes
    nothing: a direct dial of the port and a request that came through the edge get the same refusal."""
    name, seen = upstream
    r = TestClient(cp_mod.app, PUBLIC_BINDS[0]).post(f"/m/{name}/api/agent/chat", content=b"{}", headers=spoof)
    assert r.status_code == 403, spoof
    assert [p for p in seen if p[0] == "POST"] == []


def test_an_explicit_operator_opt_in_opens_the_write(cp_mod, upstream, monkeypatch):
    name, seen = upstream
    monkeypatch.setattr(cp_mod, "DEMO_ALLOW_WRITES", True)
    r = TestClient(cp_mod.app, PUBLIC_BINDS[0]).post(f"/m/{name}/api/agent/chat", content=b"{}")
    assert r.status_code == 200, r.text
    assert ("POST", "/api/agent/chat") in seen


def test_demo_read_only_still_refuses_even_on_loopback(cp_mod, upstream, monkeypatch):
    """The older, opt-in posture keeps working exactly as documented — narrowed, not replaced."""
    name, seen = upstream
    monkeypatch.setattr(cp_mod, "DEMO_READ_ONLY", True)
    r = TestClient(cp_mod.app, LOOPBACK).post(f"/m/{name}/api/agent/chat", content=b"{}")
    assert r.status_code == 403 and r.json()["read_only"] is True
    assert [p for p in seen if p[0] == "POST"] == []


def test_a_traversal_is_still_a_400_before_any_write_policy(cp_mod, upstream):
    """Order matters for truthfulness: a malformed path gets the same answer on every bind, so the route
    never leaks whether writes happen to be open here."""
    name, seen = upstream
    for tail in ("..%2fother%2fx", "%2e%2e/other/x", "../other/x"):
        r = TestClient(cp_mod.app, PUBLIC_BINDS[0]).post(f"/m/{name}/api/{tail}", content=b"{}")
        assert r.status_code in (400, 404), tail
    assert seen == [], "nothing — malformed or refused — may reach the module"


@pytest.mark.parametrize("host,expect", [
    ("127.0.0.1", True), ("127.0.0.53", True), ("::1", True),
    ("0.0.0.0", False), ("::", False), ("10.0.0.7", False), ("192.168.1.9", False),
    ("testserver", False), ("localhost", False), ("", False),
])
def test_bind_classification_is_by_ip_not_by_name(host, expect):
    """uvicorn reports the resolved address it bound, so a bare name is never evidence of loopback;
    treating `localhost` or TestClient's `testserver` as safe would be a guess, so it is refused."""
    from agentic_os.control_plane import _arrived_on_loopback
    assert _arrived_on_loopback({"server": (host, 8080)}) is expect


def test_a_scope_without_a_usable_server_address_is_treated_as_public():
    from agentic_os.control_plane import _arrived_on_loopback
    assert _arrived_on_loopback({}) is False
    assert _arrived_on_loopback({"server": None}) is False
    assert _arrived_on_loopback({"server": ("", 0)}) is False

