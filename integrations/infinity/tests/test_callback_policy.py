"""The callback URL is untrusted input. No arbitrary callback URLs: SSRF, DNS rebinding and redirects are refused."""
import http.server
import threading

import pytest

from redevops_infinity.callback_auth import (AuthError, CallbackClient, CallbackOrigin, CallbackRefused, CallbackVerifier, Peer, callback_headers,
                                             check_callback_url)

LOOP = (CallbackOrigin("http://127.0.0.1:8099", allow_insecure_loopback=True),)
PUB = (CallbackOrigin("https://agent.example.org"),)
PUBIP = lambda host: ["93.184.216.34"]  # noqa: E731


@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1:8099@evil.example/cb", "invalid_url"),                         # credentials
    ("http://user:pw@127.0.0.1:8099/cb", "invalid_url"),
    ("http://127.0.0.1:8099/cb#frag", "invalid_url"),
    ("http://127.0.0.1:8099/cb x", "invalid_url"),
    ("http://127.0.0.1:8099/" + "a" * 600, "invalid_url"),
    ("ftp://127.0.0.1:8099/cb", "invalid_url"),
    ("http://127.0.0.1:8100/cb", "origin_not_allowlisted"),                           # another port
    ("http://127.0.0.1/cb", "origin_not_allowlisted"),
    ("https://127.0.0.1:8099/cb", "origin_not_allowlisted"),
    ("http://localhost:8099/cb", "origin_not_allowlisted"),                           # a name, not the allowlisted literal
    ("http://169.254.169.254/latest/meta-data/", "origin_not_allowlisted"),           # cloud metadata
    ("http://2130706433:8099/cb", "non_canonical_address"),                           # decimal form of 127.0.0.1
    ("http://0x7f.0.0.1:8099/cb", "non_canonical_address"),
    ("http://127.1:8099/cb", "non_canonical_address"),
    ("http://[::1]:8099/cb", "origin_not_allowlisted"),
    ("", "invalid_url"), (None, "invalid_url"), (5, "invalid_url"),
])
def test_loopback_policy_refuses(url, reason):
    with pytest.raises(CallbackRefused) as e:
        check_callback_url(url, LOOP)
    assert e.value.reason == reason


def test_the_allowlisted_loopback_literal_is_accepted():
    assert check_callback_url("http://127.0.0.1:8099/cb/abc?x=1", LOOP) == ("http://127.0.0.1:8099", ["127.0.0.1"])


def test_plain_http_is_never_allowed_to_a_non_loopback_origin():
    with pytest.raises(ValueError):
        CallbackOrigin("http://agent.example.org")
    with pytest.raises(ValueError):
        CallbackOrigin("http://10.0.0.5:80", allow_insecure_loopback=True)
    with pytest.raises(ValueError):
        CallbackOrigin("https://agent.example.org/path")


def test_a_public_name_must_resolve_only_to_public_addresses():
    assert check_callback_url("https://agent.example.org/cb", PUB, PUBIP)[1] == ["93.184.216.34"]
    for bad in (["10.0.0.7"], ["127.0.0.1"], ["169.254.169.254"], ["93.184.216.34", "192.168.1.5"], ["::1"], ["::ffff:10.0.0.1"], ["fd00::1"]):
        with pytest.raises(CallbackRefused) as e:
            check_callback_url("https://agent.example.org/cb", PUB, lambda h, b=bad: b)
        assert e.value.reason == "non_public_address", bad
    with pytest.raises(CallbackRefused):
        check_callback_url("https://agent.example.org/cb", PUB, lambda h: [])
    def boom(h): raise OSError("nxdomain")                               # noqa: E704
    with pytest.raises(CallbackRefused):
        check_callback_url("https://agent.example.org/cb", PUB, boom)


def test_private_addresses_need_an_explicit_entry():
    internal = (CallbackOrigin("https://runtime.internal.example", allow_private=True),)
    assert check_callback_url("https://runtime.internal.example/cb", internal, lambda h: ["10.1.2.3"])[1] == ["10.1.2.3"]


class _Redirector(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):                                  # noqa: N802
        _Redirector.hits.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/internal")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a): return                   # noqa: E704


def test_redirects_are_not_followed():
    _Redirector.hits = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.05), daemon=True).start()
    try:
        port = httpd.server_address[1]
        out = CallbackClient().post(f"http://127.0.0.1:{port}/cb", b"{}", {}, (CallbackOrigin(f"http://127.0.0.1:{port}", allow_insecure_loopback=True),))
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert out.kind == "refused" and out.detail == "redirect_not_followed" and _Redirector.hits == ["/cb"]


def test_the_connection_is_pinned_to_the_checked_address():
    """DNS rebinding: the name is resolved once, checked, and the socket connects to that address; a second answer is never consulted."""
    answers = iter([["127.0.0.1"], ["10.9.9.9"]])
    seen = []
    import socket
    real = socket.create_connection

    def spy(addr, *a, **k):
        seen.append(addr[0])
        return real(addr, *a, **k)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.05), daemon=True).start()
    port = httpd.server_address[1]
    socket.create_connection = spy
    try:
        c = CallbackClient(resolver=lambda h: next(answers))
        out = c.post(f"https://rebind.example:{port}/cb", b"{}", {}, (CallbackOrigin(f"https://rebind.example:{port}", allow_private=True),))
    finally:
        socket.create_connection = real
        httpd.shutdown()
        httpd.server_close()
    assert seen == ["127.0.0.1"] and out.kind in ("retry", "permanent", "refused")


def test_a_delivery_to_a_non_allowlisted_origin_is_refused_without_connecting():
    out = CallbackClient().post("http://127.0.0.1:1/cb", b"{}", {}, LOOP)
    assert out.kind == "refused" and out.detail == "origin_not_allowlisted"


# ── authenticated callbacks (the receiver side) ─────────────────────────────────────────────────────────────────
SECRET = b"s" * 32


def _peer(**kw):
    base = dict(key_id="k1", secret=SECRET, tenant="t1", principal="p1", audience="bridge")
    base.update(kw)
    return Peer(**base)


def _hdr(peer, url="http://127.0.0.1:8099/cb/abc", body=b'{"a":1}', **kw):
    d = dict(url=url, body=body, event_id="evt_1", operation="update_lead", group_id="g", tool_call_id="c", expires_at=2_100_000, now=2_000_000)
    d.update(kw)
    return callback_headers(peer, **d)


def _ver(**kw):
    base = dict(expect_tenant="t1", clock=lambda: 2_000_000.0)
    base.update(kw)
    return CallbackVerifier(SECRET, "k1", "http://127.0.0.1:8099", **base)


def test_a_callback_verifies_and_a_duplicate_is_acknowledged_not_applied():
    v, h = _ver(), _hdr(_peer())
    assert v.verify("/cb/abc", h, b'{"a":1}')[0] == "ok"
    h2 = _hdr(_peer())                                              # a retry: same event id, fresh nonce
    assert v.verify("/cb/abc", h2, b'{"a":1}')[0] == "duplicate"


def test_callback_replay_tamper_substitution_and_expiry_are_refused():
    body = b'{"a":1}'
    h = _hdr(_peer())
    v = _ver()
    v.verify("/cb/abc", h, body)
    with pytest.raises(AuthError):                                  # the same message replayed (same nonce)
        v.verify("/cb/abc", h, body)
    with pytest.raises(AuthError):                                  # body tampered
        _ver().verify("/cb/abc", _hdr(_peer()), b'{"a":2}')
    with pytest.raises(AuthError):                                  # another path (a different call's callback URL)
        _ver().verify("/cb/other", _hdr(_peer()), body)
    with pytest.raises(AuthError) as e:                             # cross-tenant substitution: the receiver expects tenant t1
        _ver().verify("/cb/abc", _hdr(_peer(tenant="t2")), body)
    assert e.value.status in (401, 403)
    with pytest.raises(AuthError):                                  # expired message
        _ver().verify("/cb/abc", _hdr(_peer(), expires_at=1_999_000), body)
    with pytest.raises(AuthError):                                  # stale timestamp
        _ver().verify("/cb/abc", _hdr(_peer(), now=1_000_000), body)
    with pytest.raises(AuthError):                                  # another audience (origin)
        CallbackVerifier(SECRET, "k1", "http://127.0.0.1:9999", expect_tenant="t1", clock=lambda: 2_000_000.0).verify("/cb/abc", _hdr(_peer()), body)
    with pytest.raises(AuthError):                                  # wrong key
        CallbackVerifier(b"z" * 32, "k1", "http://127.0.0.1:8099", expect_tenant="t1", clock=lambda: 2_000_000.0).verify("/cb/abc", _hdr(_peer()), body)


def test_an_event_id_cannot_be_reused_for_a_different_body():
    v = _ver()
    v.verify("/cb/abc", _hdr(_peer()), b'{"a":1}')
    with pytest.raises(AuthError) as e:
        v.verify("/cb/abc", _hdr(_peer(), body=b'{"a":2}'), b'{"a":2}')
    assert e.value.status == 409


def test_every_bound_field_is_in_the_signature():
    body = b'{"a":1}'
    for field, bad in (("operation", "other_op"), ("group_id", "g2"), ("tool_call_id", "c2"), ("event_id", "evt_2")):
        h = _hdr(_peer())
        h[{"operation": "X-Rap-Operation", "group_id": "X-Rap-Group-Id", "tool_call_id": "X-Rap-Tool-Call-Id", "event_id": "X-Rap-Event-Id"}[field]] = bad
        with pytest.raises(AuthError):
            _ver().verify("/cb/abc", h, body)
