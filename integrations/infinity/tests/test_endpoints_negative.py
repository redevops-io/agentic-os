"""Every exposed endpoint: deny by default, authenticated, scoped, rate limited, bounded. One parametrised matrix over all four."""
import http.client
import json

import pytest

from redevops_infinity import RapHttpServer, RateLimiter
from redevops_infinity.callback_auth import H_AUD, H_KEY, H_NONCE, H_SIG, H_TS, sign_request
from redevops_infinity.contracts import PATH_CANCEL, PATH_CLOSE, PATH_INVOKE, RAP_TOOLSET_PATH
from redevops_infinity.demo.env import AUDIENCE, KEY_ID, SECRET

ENDPOINTS = [("GET", RAP_TOOLSET_PATH, b""), ("POST", PATH_INVOKE, b'{"operation":"lookup_lead"}'),
             ("POST", PATH_CANCEL, b'{"thread_id":"t","tool_call_id":"c"}'), ("POST", PATH_CLOSE, b'{"thread_id":"t"}')]
IDS = [p for _, p, _ in ENDPOINTS]


def call(srv, method, path, body=b"", headers=None, content_type="application/json", raw_headers=None):
    conn = http.client.HTTPConnection(srv.host, srv.port, timeout=10)
    try:
        h = dict(headers or {})
        if content_type:
            h["Content-Type"] = content_type
        h.update(raw_headers or {})
        conn.request(method, path, body=body if body else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        return r.status, data
    finally:
        conn.close()


def signed(clock, method, path, body, **kw):
    return sign_request(kw.pop("secret", SECRET), kw.pop("key_id", KEY_ID), kw.pop("audience", AUDIENCE), method, path, body, ts=int(clock()), **kw)


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_no_credentials_is_401_and_reveals_nothing(runtime_factory, method, path, body):
    env, srv, rt = runtime_factory()
    st, data = call(srv, method, path, body)
    assert st == 401 and json.loads(data) == {"error": "unauthorized"}


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_unknown_paths_and_methods_are_401_before_they_are_404(runtime_factory, method, path, body):
    env, srv, rt = runtime_factory()
    for m, p in (("GET", "/admin"), ("POST", "/admin"), ("DELETE", path), ("PUT", path), ("GET", "/"), ("POST", path + "/x"), ("OPTIONS", path), ("HEAD", path)):
        st, _ = call(srv, m, p, b"{}" if m in ("POST", "PUT") else b"")
        assert st == 401, (m, p, st)


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_a_bad_signature_is_refused(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    h = signed(clock, method, path, body)
    h[H_SIG] = ("0" if h[H_SIG][0] != "0" else "1") + h[H_SIG][1:]
    assert call(srv, method, path, body, h)[0] == 401
    assert call(srv, method, path, body, signed(clock, method, path, body, secret=b"x" * 32))[0] == 401
    assert call(srv, method, path, body, signed(clock, method, path, body, key_id="nobody"))[0] == 401


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_the_signature_binds_method_path_body_and_audience(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    h = signed(clock, method, path, body)
    other_path = PATH_CLOSE if path != PATH_CLOSE else PATH_CANCEL
    assert call(srv, "POST", other_path, b"{}", h)[0] == 401                          # a signed request cannot be replayed at another endpoint
    assert call(srv, method, path, body + b" ", signed(clock, method, path, body))[0] in (400, 401, 411)   # tampered body
    assert call(srv, method, path, body, signed(clock, method, path, body, audience="another-service"))[0] == 401
    h2 = signed(clock, method, path, body)
    h2[H_AUD] = "another-service"
    assert call(srv, method, path, body, h2)[0] == 401


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_a_replayed_request_is_refused(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    h = signed(clock, method, path, body)
    first = call(srv, method, path, body, h)[0]
    assert first in (200, 400)
    assert call(srv, method, path, body, h)[0] == 401


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_clock_skew_is_refused_both_ways(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    for off in (-3600, 3600):
        h = sign_request(SECRET, KEY_ID, AUDIENCE, method, path, body, ts=int(clock()) + off)
        assert call(srv, method, path, body, h)[0] == 401


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_revoked_and_expired_keys_are_refused(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    assert call(srv, method, path, body, signed(clock, method, path, body))[0] != 401
    env.peer.expires_at = clock() + 10
    assert call(srv, method, path, body, signed(clock, method, path, body))[0] != 401
    clock.advance(20)
    assert call(srv, method, path, body, signed(clock, method, path, body))[0] == 401
    env.peer.expires_at = 0
    env.peers.revoke(KEY_ID)
    assert call(srv, method, path, body, signed(clock, method, path, body))[0] == 401


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_a_peer_without_the_scope_gets_403(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    scope = {RAP_TOOLSET_PATH: "discover", PATH_INVOKE: "invoke", PATH_CANCEL: "cancel", PATH_CLOSE: "close"}[path]
    env.peer.scopes = frozenset(env.peer.scopes - {scope})
    assert call(srv, method, path, body, signed(clock, method, path, body))[0] == 403


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_authenticated_unsupported_methods_and_paths(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    wrong = "POST" if method == "GET" else "GET"
    assert call(srv, wrong, path, b"", signed(clock, wrong, path, b""))[0] in (403, 405)
    assert call(srv, "DELETE", path, b"", signed(clock, "DELETE", path, b""))[0] in (403, 405)
    st, _ = call(srv, "GET", "/admin", b"", signed(clock, "GET", "/admin", b""))
    assert st in (403, 404)


@pytest.mark.parametrize("path", [PATH_INVOKE, PATH_CANCEL, PATH_CLOSE])
def test_bodies_are_bounded_and_must_be_json(runtime_factory, clock, path):
    env, srv, rt = runtime_factory()
    big = b"{" + b'"a":"' + b"x" * 70000 + b'"}'
    assert call(srv, "POST", path, big, signed(clock, "POST", path, big))[0] == 413
    ok = b'{"thread_id":"t","tool_call_id":"c"}'
    assert call(srv, "POST", path, ok, signed(clock, "POST", path, ok), content_type="text/plain")[0] == 415
    assert call(srv, "POST", path, b"{not json", signed(clock, "POST", path, b"{not json"))[0] in (200, 400)   # cancel/close are always 200 by specification
    chunked = call(srv, "POST", path, ok, signed(clock, "POST", path, ok), raw_headers={"Transfer-Encoding": "chunked"})
    assert chunked[0] in (400, 411)


@pytest.mark.parametrize("method,path,body", ENDPOINTS, ids=IDS)
def test_requests_are_rate_limited_per_source_and_per_peer(runtime_factory, clock, method, path, body):
    env, srv, rt = runtime_factory()
    srv.limiter = RateLimiter(clock=lambda: 0.0)          # frozen time: no refill
    srv.ip_rate, srv.ip_burst = 0.0, 5
    codes = [call(srv, method, path, body)[0] for _ in range(8)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429] * 3
    srv.limiter = RateLimiter(clock=lambda: 0.0)
    srv.ip_rate, srv.ip_burst = 1e9, 10 ** 6
    env.peer.rate_per_s, env.peer.burst = 0.0, 2
    codes = [call(srv, method, path, body, signed(clock, method, path, body))[0] for _ in range(4)]
    assert codes[2:] == [429, 429] and 429 not in codes[:2]


def test_the_listener_binds_to_loopback_by_default(runtime_factory):
    env, srv, rt = runtime_factory()
    assert srv.host == "127.0.0.1"


def test_error_replies_never_include_internals(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    for st, data in (call(srv, "POST", PATH_INVOKE, b"{}"), call(srv, "POST", PATH_INVOKE, b"{}", signed(clock, "POST", PATH_INVOKE, b"{}"))):
        text = data.decode()
        assert len(text) < 80 and "Traceback" not in text and "redevops" not in text
