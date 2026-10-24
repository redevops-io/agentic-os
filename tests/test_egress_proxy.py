"""The egress proxy: deny by default, SSRF-safe, one JSON line per decision. Real sockets, no Docker."""
from __future__ import annotations

import asyncio
import http.server
import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("egress_proxy", ROOT / "deploy" / "egress" / "proxy.py")
ep = importlib.util.module_from_spec(spec)
sys.modules["egress_proxy"] = ep
spec.loader.exec_module(ep)


# ── policy grammar and decisions ─────────────────────────────────────────────────────────
def test_default_is_deny():
    p = ep.Policy.parse("")
    assert not p.decide("example.com", 443).allow and "not on the allowlist" in p.decide("example.com", 443).reason


def test_exact_host_default_port_443_only():
    p = ep.Policy.parse("api.example.com")
    assert p.decide("api.example.com", 443).allow
    d = p.decide("api.example.com", 80)
    assert not d.allow and "port 80" in d.reason
    assert not p.decide("other.example.com", 443).allow


def test_case_and_trailing_dot_are_normalised():
    p = ep.Policy.parse("API.Example.COM.")
    assert p.decide("api.example.com", 443).allow and p.decide("Api.Example.Com.", 443).allow


def test_wildcard_matches_subdomains_never_the_apex_or_lookalikes():
    p = ep.Policy.parse("*.googleapis.com")
    assert p.decide("mail.googleapis.com", 443).allow and p.decide("a.b.googleapis.com", 443).allow
    for bad in ("googleapis.com", "evilgoogleapis.com", "googleapis.com.evil.io", "x.googleapis.com.evil.io"):
        assert not p.decide(bad, 443).allow, bad


def test_explicit_ports_and_private_flag():
    p = ep.Policy.parse("host.docker.internal:8000 private  # the model server\napi.x.io:443,8443")
    assert p.decide("host.docker.internal", 8000).private_ok and not p.decide("api.x.io", 8443).private_ok
    assert p.decide("api.x.io", 8443).allow and not p.decide("api.x.io", 9000).allow


@pytest.mark.parametrize("line", ["a.com bogus", "*.*.com", "a*.com", "a.com:notaport", "a.com:70000", "bad host.com"])
def test_malformed_rules_are_rejected_not_guessed(line):
    with pytest.raises(ValueError):
        ep.Policy.parse(line)


# ── SSRF guard ───────────────────────────────────────────────────────────────────────────
def pick(host, rules, answers, port=443):
    pol = ep.Policy.parse(rules)
    return ep.pick_address(host, port, pol.decide(host, port), lambda h, p: answers)


@pytest.mark.parametrize("addr", ["10.0.0.5", "192.168.1.1", "172.16.0.9", "127.0.0.1", "169.254.169.254", "100.64.0.1",
                                  "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1"])
def test_a_wildcard_or_exact_rule_never_reaches_a_non_public_address(addr):
    a, why = pick("api.example.com", "api.example.com", [addr])
    assert a is None and "non-public" in why


def test_a_mixed_answer_is_refused_so_rebinding_cannot_slip_a_private_address_in():
    a, why = pick("api.example.com", "api.example.com", ["93.184.216.34", "10.0.0.5"])
    assert a is None


def test_the_connection_goes_to_the_address_that_was_checked():
    a, _ = pick("api.example.com", "api.example.com", ["93.184.216.34", "93.184.216.35"])
    assert a == "93.184.216.34"


def test_an_explicit_private_rule_allows_exactly_that_target():
    a, _ = pick("model.internal", "model.internal:8000 private", ["10.1.2.3"], port=8000)
    assert a == "10.1.2.3"


def test_a_literal_ip_needs_an_exact_rule_and_the_metadata_address_is_never_reachable_by_accident():
    pol = ep.Policy.parse("*.example.com")
    assert not pol.decide("169.254.169.254", 80).allow
    assert not ep.Policy.parse("93.184.216.34").decide("93.184.216.35", 443).allow
    a, _ = pick("93.184.216.34", "93.184.216.34", [])
    assert a == "93.184.216.34"
    a, why = pick("169.254.169.254", "169.254.169.254:80", [], port=80)
    assert a is None and "non-public" in why


def test_unresolvable_names_are_refused():
    def boom(h, p):
        raise OSError("nxdomain")
    pol = ep.Policy.parse("nope.example.com")
    a, why = ep.pick_address("nope.example.com", 443, pol.decide("nope.example.com", 443), boom)
    assert a is None and "cannot resolve" in why


# ── the proxy over real sockets ──────────────────────────────────────────────────────────
class Origin:
    """A loopback TCP echo server and an HTTP server, so tests can prove whether the proxy contacted them."""

    def __init__(self):
        self.tcp_hits = 0
        self.http_hits = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.http_hits.append(("GET", self.path, dict(self.headers)))
                body = b"hello from origin"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(n)
                outer.http_hits.append(("POST", self.path, data))
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.http_port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tcp = socket.socket(); self.tcp.bind(("127.0.0.1", 0)); self.tcp.listen(8)
        self.tcp_port = self.tcp.getsockname()[1]
        threading.Thread(target=self._echo, daemon=True).start()

    def _echo(self):
        while True:
            try:
                c, _ = self.tcp.accept()
            except OSError:
                return
            self.tcp_hits += 1
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    @staticmethod
    def _one(c):
        try:
            while (d := c.recv(4096)):
                c.sendall(d)
        finally:
            c.close()

    def close(self):
        self.httpd.shutdown(); self.tcp.close()


class Running:
    def __init__(self, rules: str, **kw):
        self.logs = []
        self.policy = ep.Policy.parse(rules)
        self.loop = asyncio.new_event_loop()
        self.proxy = ep.Proxy(self.policy, log=self.logs.append, **kw)
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.server = self.loop.run_until_complete(ep.serve("127.0.0.1", 0, self.proxy))
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start()
        assert ready.wait(5)

    def stop(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def world():
    origin = Origin()
    made = []

    def make(rules, **kw):
        r = Running(rules, **kw)
        made.append(r)
        return r
    yield origin, make
    for r in made:
        r.stop()
    origin.close()


def raw(port, payload: bytes, read=True, timeout=5):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(payload)
    out = b""
    if read:
        try:
            while (d := s.recv(65536)):
                out += d
        except socket.timeout:
            pass
    s.close()
    return out


def wait_logs(r, n=1):
    for _ in range(100):
        if len(r.logs) >= n:
            return r.logs
        time.sleep(0.02)
    return r.logs


def test_connect_to_an_allowed_private_target_tunnels_bytes(world):
    origin, make = world
    r = make(f"127.0.0.1:{origin.tcp_port},{origin.http_port} private")
    s = socket.create_connection(("127.0.0.1", r.port), timeout=5)
    s.sendall(f"CONNECT 127.0.0.1:{origin.tcp_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert s.recv(200).startswith(b"HTTP/1.1 200")
    s.sendall(b"ping-through-the-tunnel")
    assert s.recv(100) == b"ping-through-the-tunnel"
    s.close()
    logs = wait_logs(r)
    assert logs[0]["allow"] is True and logs[0]["method"] == "CONNECT" and logs[0]["host"] == "127.0.0.1" and logs[0]["up"] > 0


def test_connect_to_a_host_off_the_allowlist_is_403_and_the_origin_is_never_contacted(world):
    origin, make = world
    r = make("api.example.com")
    out = raw(r.port, f"CONNECT 127.0.0.1:{origin.tcp_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403") and b"not on the allowlist" in out
    time.sleep(0.1)
    assert origin.tcp_hits == 0
    log = wait_logs(r)[0]
    assert log["allow"] is False and "allowlist" in log["reason"]


def test_connect_to_a_loopback_origin_is_refused_unless_the_rule_says_private(world):
    origin, make = world
    r = make(f"127.0.0.1:{origin.tcp_port}")           # listed, but NOT marked private
    out = raw(r.port, f"CONNECT 127.0.0.1:{origin.tcp_port} HTTP/1.1\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403") and b"non-public" in out and origin.tcp_hits == 0


def test_connect_to_a_disallowed_port_of_an_allowed_host_is_refused(world):
    origin, make = world
    r = make(f"127.0.0.1:{origin.tcp_port} private")
    out = raw(r.port, f"CONNECT 127.0.0.1:{origin.http_port} HTTP/1.1\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403") and b"port" in out


def test_plain_http_get_and_post_are_forwarded_when_allowed(world):
    origin, make = world
    r = make(f"127.0.0.1:{origin.http_port} private")
    out = raw(r.port, f"GET http://127.0.0.1:{origin.http_port}/path?q=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      f"Proxy-Connection: keep-alive\r\nX-Custom: yes\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.0 200") or out.startswith(b"HTTP/1.1 200")
    assert out.endswith(b"hello from origin")
    method, path, headers = origin.http_hits[0]
    assert (method, path) == ("GET", "/path?q=1") and headers.get("X-Custom") == "yes"
    assert "Proxy-Connection" not in headers                                            # hop-by-hop header not forwarded
    body = b'{"n": 1}'
    out = raw(r.port, f"POST http://127.0.0.1:{origin.http_port}/p HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    assert out.endswith(body) and origin.http_hits[1][2] == body


def test_plain_http_to_a_host_off_the_list_is_403(world):
    origin, make = world
    r = make("api.example.com:80")
    out = raw(r.port, f"GET http://127.0.0.1:{origin.http_port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 403") and origin.http_hits == []


def test_the_identity_header_is_recorded_in_the_decision_log(world):
    origin, make = world
    r = make("api.example.com")
    raw(r.port, b"CONNECT blocked.example.org:443 HTTP/1.1\r\nX-Egress-Identity: revenue-agent\r\n\r\n")
    log = wait_logs(r)[0]
    assert log["identity"] == "revenue-agent" and log["host"] == "blocked.example.org" and log["port"] == 443
    assert {"ts", "client", "method", "allow", "rule", "reason", "up", "down", "ms"} <= set(log)
    json.dumps(log)                                                                      # one JSON line per decision


def test_not_a_proxy_request_and_malformed_input_are_400_and_health_is_ok(world):
    origin, make = world
    r = make("api.example.com")
    assert raw(r.port, b"GET /something HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert raw(r.port, b"garbage\r\n\r\n").startswith(b"HTTP/1.1 400")
    health = raw(r.port, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
    assert health.startswith(b"HTTP/1.1 200") and b'"ok": true' in health


def test_an_oversized_header_is_rejected(world):
    origin, make = world
    r = make("api.example.com")
    big = b"CONNECT a.example.com:443 HTTP/1.1\r\nX-Pad: " + b"a" * 40_000 + b"\r\n\r\n"
    assert raw(r.port, big).startswith(b"HTTP/1.1 431")


def test_an_idle_tunnel_is_closed(world):
    origin, make = world
    r = make(f"127.0.0.1:{origin.tcp_port} private", idle_s=0.3)
    s = socket.create_connection(("127.0.0.1", r.port), timeout=5)
    s.sendall(f"CONNECT 127.0.0.1:{origin.tcp_port} HTTP/1.1\r\n\r\n".encode())
    assert s.recv(100).startswith(b"HTTP/1.1 200")
    time.sleep(0.8)
    assert s.recv(100) == b""                                                             # the proxy hung up
    s.close()


def test_reloading_the_allowlist_takes_effect_and_a_bad_reload_keeps_the_last_good_policy(world):
    origin, make = world
    r = make("api.example.com")
    assert raw(r.port, f"CONNECT 127.0.0.1:{origin.tcp_port} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")
    r.proxy.reload(ep.Policy.parse(f"127.0.0.1:{origin.tcp_port} private"))
    ep._reload(r.proxy, "/definitely/not/a/file")                                          # must not raise or clear the policy
    assert r.proxy.policy.decide("127.0.0.1", origin.tcp_port).allow


def test_the_cli_validates_an_allowlist(tmp_path, capsys):
    good = tmp_path / "ok.txt"
    good.write_text("api.example.com\n*.googleapis.com\n")
    assert ep.main(["--allowlist", str(good), "--check"]) == 0
    bad = tmp_path / "bad.txt"
    bad.write_text("*.*.com\n")
    assert ep.main(["--allowlist", str(bad), "--check"]) == 2
    assert ep.main(["--allowlist", str(tmp_path / "missing.txt"), "--check"]) == 2
