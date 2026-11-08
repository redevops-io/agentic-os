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
import socket
import threading
from pathlib import Path

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
# TestClient's base_url decides what the request's `server` scope says the connection was delivered on —
# that is the value the gate reads, so this is how a bind is simulated here. 10.0.0.7 is the realistic
# public case (a LAN or container-interface address, e.g. the 172.17.0.4 a Docker network reports);
# 0.0.0.0 is not a value a real server reports (uvicorn reports the accepted socket's address, verified in
# the real-socket tests below) and is kept deliberately to prove any non-loopback value fails closed.
# An IPv6 host cannot be expressed this way at all — Starlette parses base_url by splitting on ":" — so
# "::" and "::1" are asserted on the classifier directly.
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


def test_a_refusal_changes_no_persistent_state(cp_mod, upstream, tmp_path):
    """403 is not enough: a refused write must leave nothing behind, in the module or on this host.

    Every file the control plane can persist (the grant store, the home directory it writes under) is
    fingerprinted by content hash, several refused writes are issued across two public binds, and the
    fingerprint must come back byte-identical — with the module recording no request at all.
    """
    import hashlib

    def fingerprint(root):
        out = {}
        base = Path(root)
        if not base.exists():
            return {"<absent>": ""}
        for p in sorted(base.rglob("*")):
            key = str(p.relative_to(base))
            out[key] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
        return out

    perm_dir = tmp_path / "perm"
    home_dir = tmp_path / "home"
    perm_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    before = (fingerprint(perm_dir), fingerprint(home_dir))
    name, seen = upstream
    for bind in PUBLIC_BINDS:
        c = TestClient(cp_mod.app, base_url=bind)
        for tail in ("agent/chat", "agent/run", "billing/post", "x"):
            assert c.post(f"/m/{name}/api/{tail}", content=b'{"text":"go"}').status_code == 403, tail
        assert c.post(f"/m/{name}/api/agent/chat", content=b"{}",
                      headers={"host": "localhost"}).status_code == 403
    assert (fingerprint(perm_dir), fingerprint(home_dir)) == before, "a refused write persisted something"
    assert seen == [], "a refused write reached the module"


@pytest.mark.parametrize("host,expect", [
    ("127.0.0.1", True), ("127.0.0.53", True), ("::1", True),
    ("0.0.0.0", False), ("::", False), ("10.0.0.7", False), ("192.168.1.9", False),
    ("172.17.0.4", False),        # what a Docker container interface reports — the hosted-demo value
    ("testserver", False), ("localhost", False), ("", False),
])
def test_bind_classification_is_by_ip_not_by_name(host, expect):
    """A bare name is never evidence of loopback. uvicorn reports the accepted socket's local address here
    (verified in the real-socket tests below), so `localhost` and TestClient's `testserver` would be a
    guess about a value a real server never produces — and a guess in the permissive direction is refused."""
    from agentic_os.control_plane import _arrived_on_loopback
    assert _arrived_on_loopback({"server": (host, 8080)}) is expect


def test_a_scope_without_a_usable_server_address_is_treated_as_public():
    from agentic_os.control_plane import _arrived_on_loopback
    assert _arrived_on_loopback({}) is False
    assert _arrived_on_loopback({"server": None}) is False
    assert _arrived_on_loopback({"server": ("", 0)}) is False


# ── the mechanism itself, over a real socket ───────────────────────────────────────────────────────
# Everything above drives TestClient, whose `server` value comes from base_url — it is a claim about the
# classifier, not about uvicorn. These two run a real uvicorn over a real TCP connection, because the gate
# is only as good as what the server actually puts in scope["server"].

_ECHO = '''
import json
from starlette.responses import JSONResponse
async def app(scope, receive, send):
    body = json.dumps({"server": list(scope.get("server", []))}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
'''


def _free_port(host="127.0.0.1"):
    import socket
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM); s.bind((host, 0)); p = s.getsockname()[1]; s.close(); return p


def _serve(tmp_path, source, port, host="127.0.0.1"):
    import subprocess, sys, time
    (tmp_path / "echo_app.py").write_text(source)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "echo_app:app", "--host", host,
                             "--port", str(port), "--log-level", "critical"],
                            cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):                                   # up to 8s for the socket to open
            try:
                import socket
                with socket.create_connection((host, port), timeout=0.5):
                    return proc
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
    except Exception:
        pass
    proc.terminate()
    pytest.skip("could not start a real uvicorn for the socket-semantics check")


def test_a_real_server_reports_the_accepted_local_address_as_the_server_scope(tmp_path):
    """A 0.0.0.0 bind dialled from loopback reports 127.0.0.1, not the bind string — which is why the rule
    is 'arrived over loopback' rather than 'bound to loopback', and why it holds for IPv6 too."""
    import json
    import urllib.request
    for bind, dial in (("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1")):
        port = _free_port()
        proc = _serve(tmp_path, _ECHO, port)
        try:
            with urllib.request.urlopen(f"http://{dial}:{port}/", timeout=10) as r:
                reported = json.loads(r.read())["server"][0]
            assert reported == "127.0.0.1", f"bind {bind}: scope['server'] said {reported}"
        finally:
            proc.terminate(); proc.wait(timeout=10)


def test_a_real_ipv6_loopback_connection_reports_and_reads_as_loopback(tmp_path, cp_mod):
    """The IPv6 half of the same claim, over a real socket.

    TestClient cannot express an IPv6 base_url (Starlette splits it on ":"), so before this the `::1` branch
    of the gate rested on hand-written scope values. Here a real uvicorn on `::` is dialled at `::1`: the
    server reports `::1` and the classifier calls it loopback — and a `::`-bind request that arrived on a
    routable v6 address is not loopback.

    It takes `cp_mod` for the environment isolation it provides (a scratch permissions dir and home), not for
    the module: importing `control_plane` cold, as this file's other real-socket tests never do, reaches the
    default grant store, which refuses to run unencrypted outside a provisioned host.
    """
    import json
    import urllib.request
    from agentic_os.control_plane import _arrived_on_loopback
    port = _free_port("::1")
    proc = _serve(tmp_path, _ECHO, port, host="::")
    try:
        with urllib.request.urlopen(f"http://[::1]:{port}/", timeout=10) as r:
            reported = json.loads(r.read())["server"][0]
    finally:
        proc.terminate(); proc.wait(timeout=10)
    assert reported == "::1", f"a :: bind dialled at ::1 reported {reported!r}"
    assert _arrived_on_loopback({"server": (reported, port)}) is True
    # the same wildcard bind, arrived on over a routable v6 address: not loopback, so writes are refused
    assert _arrived_on_loopback({"server": ("2a01:4f8:1234::9", port)}) is False


def test_a_real_server_forwards_a_loopback_write_end_to_end(tmp_path, cp_mod, upstream):
    """The whole route over a real TCP connection: real uvicorn, real socket, no TestClient anywhere.

    The refusal side is covered above by driving the classifier with the address values a real server
    produces (a container's 172.17.0.4, a LAN address) — dialling a non-loopback address from this test
    host is blocked by the host firewall, so it is asserted where it can be asserted honestly rather than
    faked here.
    """
    import os
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request

    name, seen = upstream
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(cp_mod.__file__)))
    perm_dir = tmp_path / "perm"
    override = "MODULE_SERVICE_" + name.replace("-", "_").replace(".", "_")
    env = {**os.environ, "PYTHONPATH": repo_root, "PERMISSIONS_DIR": str(perm_dir),
           "AGENTIC_OS_HOME": str(tmp_path / "home"), "PERMISSIONS_ALLOW_GENERATED_KEY_DEV": "1",
           override: cp_mod.MODULE_SERVICES[name]}      # the fixture already points this module at the stub
    env.pop("DEMO_READ_ONLY", None)
    env.pop("DEMO_ALLOW_WRITES", None)
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "agentic_os.control_plane:app",
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "critical"],
                            cwd=repo_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):                                  # control_plane imports slowly; allow 20s
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if proc.poll() is not None:
                    pytest.skip("control_plane would not start under uvicorn here")
                time.sleep(0.1)
        else:
            pytest.skip("control_plane did not open its socket in time")

        req = urllib.request.Request(f"http://127.0.0.1:{port}/m/{name}/api/agent/chat",
                                     data=b'{"text":"go"}', method="POST",
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            assert r.status == 200, "a write over a real loopback socket must still be forwarded"
        assert ("POST", "/api/agent/chat") in seen, "the module behind the proxy must have received it"
    except urllib.error.HTTPError as e:
        pytest.fail(f"real-socket loopback write was refused ({e.code}): {e.read()[:200]!r}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()




# ── the boundary the socket rule cannot cross (owner item, 2026-11-08) ────────────────────────────────
# These tests are not asserting an aspiration; they pin a consequence the design accepts, so nobody later
# reads "arrived over loopback" as "came from a human on this machine".

def test_a_same_host_proxy_forwarding_an_internet_request_is_treated_as_local(cp_mod, upstream):
    """Loopback-forwarded traffic may originate externally, and the gate cannot tell otherwise.

    A reverse proxy terminating on the SAME host connects to us over 127.0.0.1, so from the accepted socket
    there is nothing left to distinguish it: the request carries a public client address in X-Forwarded-For
    and CF-Connecting-IP, names the public host, and is still forwarded, because the local address the kernel
    delivered it on was loopback. No header can open the gate (see
    test_no_header_can_claim_the_request_came_from_loopback), and no header can close this gap either — the
    two tests together are the whole story of what the socket knows.

    The deployment consequence: a same-host proxy that does not authenticate its own callers is part of the
    trusted surface of this process, not a defence in front of it. If the edge authenticates (or terminates
    outside the host, as the next test covers), that is where the caller decision belongs.
    """
    name, seen = upstream
    c = TestClient(cp_mod.app, base_url=LOOPBACK)
    r = c.post(f"/m/{name}/api/agent/chat", content=b'{"text":"go"}',
               headers={"host": "demo.example", "x-forwarded-for": "203.0.113.7",
                        "cf-connecting-ip": "203.0.113.7", "x-real-ip": "203.0.113.7"})
    assert r.status_code == 200, "a same-host proxy's forwarded write is forwarded"
    assert seen, "the module saw the request the internet sent"


def test_an_edge_terminating_off_host_is_not_loopback_and_is_refused(cp_mod, upstream):
    """The other half: Cloudflare Tunnel / a container edge reaches the app on an interface address, which is
    not loopback, so the same request arriving that way is refused and reaches nobody."""
    name, seen = upstream
    c = TestClient(cp_mod.app, base_url="http://172.17.0.4:8080")
    r = c.post(f"/m/{name}/api/agent/chat", content=b'{"text":"go"}',
               headers={"cf-connecting-ip": "203.0.113.7"})
    assert r.status_code == 403
    assert seen == [], "a refusal must not reach the module"


def test_the_security_model_still_documents_the_trusted_surface():
    """The gap is a deployment fact, so it lives in the security model — and if someone edits that wording
    out, this fails rather than letting the caveat quietly disappear from the documentation."""
    doc = (Path(__file__).resolve().parents[1] / "docs" / "SECURITY_MODEL.md").read_text().lower()
    assert "loopback" in doc and ("same host" in doc or "same-host" in doc), (
        "docs/SECURITY_MODEL.md must keep stating that a same-host proxy is inside the trusted surface")


