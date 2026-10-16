"""safe_fetch: SSRF guard. Real loopback servers + injected resolvers; no outbound network."""
from __future__ import annotations

import ipaddress
import socket
import ssl

import pytest


@pytest.fixture()
def sf(mods):
    return mods.safe_fetch


def policy(sf, host="editor.test", zone="PRIVATE", port=80, **kw):
    return sf.FetchPolicy.of([sf.HostRule(host, zone, frozenset({port}))], **kw)


def res_to(ip):
    return lambda host, port: [ip]


# ── scheme / host / URL syntax ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "ftp://editor.test/x", "file:///tmp/outside.txt", "gopher://editor.test/", "javascript:alert(1)", "data:text/plain,hi",
    "//editor.test/x", "editor.test/x", "",
])
def test_non_http_schemes_and_malformed_urls_are_refused(sf, url):
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch(url, policy=policy(sf), resolver=res_to("10.0.0.5"))


def test_plain_http_requires_an_explicit_private_host(sf):
    pol = policy(sf, zone="PUBLIC", port=80)
    with pytest.raises(sf.SSRFBlocked, match="plain http"):
        sf.safe_fetch("http://editor.test/x", policy=pol, resolver=res_to("93.184.216.34"))


@pytest.mark.parametrize("url", [
    "http://user:pw@editor.test/x", "http://user@editor.test/x", "http://:pw@editor.test/x",
    "https://editor.test@other.test/x", "http://other.test\\@editor.test/",
])
def test_credentials_in_urls_and_authority_tricks_are_refused(sf, url):
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch(url, policy=policy(sf), resolver=res_to("10.0.0.5"))


@pytest.mark.parametrize("url", ["http://editor.test/x#frag", "http://editor.test/a b", "http://editor.test/\r\nHost: x",
                                 "http://editor.test/x\x00y", "http://editor.test/\\x"])
def test_fragments_whitespace_control_chars_and_backslashes_are_refused(sf, url):
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch(url, policy=policy(sf), resolver=res_to("10.0.0.5"))


def test_host_not_on_allowlist_is_refused_before_any_dns(sf):
    called = []
    with pytest.raises(sf.SSRFBlocked, match="allowlist"):
        sf.safe_fetch("http://other.test/", policy=policy(sf), resolver=lambda h, p: called.append(h) or ["10.0.0.5"])
    assert called == []                    # never even resolved


@pytest.mark.parametrize("host", ["EDITOR.TEST", "editor.test.", "Editor.Test"])
def test_host_normalisation_matches_allowlist(sf, server, host):
    srv = server(lambda r: (200, {}, b"ok"))
    pol = policy(sf, port=srv.port)
    r = sf.safe_fetch(f"http://{host}:{srv.port}/x", policy=pol, resolver=res_to("127.0.0.1"))
    assert r.status == 200 and r.body == b"ok"
    assert srv.requests[0].headers["Host"].lower().startswith("editor.test")      # original Host, pinned IP


def test_port_not_allowed_is_refused(sf):
    with pytest.raises(sf.SSRFBlocked, match="port"):
        sf.safe_fetch("http://editor.test:8081/x", policy=policy(sf, port=80), resolver=res_to("10.0.0.5"))


# ── IP literal encodings ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("host", [
    "2130706433",              # decimal 127.0.0.1
    "0x7f000001", "0x7f.0.0.1",   # hex
    "017700000001", "0177.0.0.1", "0177.0.0.01",   # octal
    "127.1", "127.0.1", "1.1.1",     # short forms
    "00127.0.0.1", "127.000.000.001",   # leading zeros
    "0", "4294967295",
])
def test_numeric_ip_encodings_are_refused_even_if_someone_allowlists_them(sf, host):
    with pytest.raises(sf.SSRFBlocked):
        sf.normalize_host(host)
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch(f"http://{host}/x", policy=policy(sf, host="editor.test"), resolver=res_to("10.0.0.5"))


@pytest.mark.parametrize("ip,zone,blocked", [
    ("127.0.0.1", "PUBLIC", True), ("127.0.0.1", "PRIVATE", False), ("10.1.2.3", "PUBLIC", True),
    ("10.1.2.3", "PRIVATE", False), ("172.16.0.1", "PUBLIC", True), ("192.168.1.1", "PUBLIC", True),
    ("192.168.1.1", "PRIVATE", False), ("100.64.0.1", "PUBLIC", True), ("100.64.0.1", "PRIVATE", False),
    ("169.254.169.254", "PUBLIC", True), ("169.254.169.254", "PRIVATE", True),       # link-local: always
    ("169.254.170.2", "PRIVATE", True), ("169.254.1.1", "PRIVATE", True),            # link-local: always
    ("100.100.100.200", "PRIVATE", True), ("192.0.0.192", "PRIVATE", True),
    ("0.0.0.0", "PRIVATE", True), ("224.0.0.1", "PRIVATE", True), ("255.255.255.255", "PRIVATE", True),
    ("240.0.0.1", "PRIVATE", True),
    ("93.184.216.34", "PUBLIC", False), ("8.8.8.8", "PUBLIC", False),
    ("::1", "PUBLIC", True), ("::1", "PRIVATE", False), ("::", "PRIVATE", True),
    ("fe80::1", "PRIVATE", True), ("fe80::1", "PUBLIC", True), ("fd00:ec2::254", "PRIVATE", True),
    ("fc00::1", "PUBLIC", True), ("fc00::1", "PRIVATE", False), ("ff02::1", "PRIVATE", True),
    ("fec0::1", "PRIVATE", True),
    ("2606:4700:4700::1111", "PUBLIC", False),
    # IPv4 smuggled inside IPv6
    ("::ffff:127.0.0.1", "PUBLIC", True), ("::ffff:169.254.169.254", "PRIVATE", True),
    ("::ffff:10.0.0.1", "PUBLIC", True), ("64:ff9b::a9fe:a9fe", "PRIVATE", True),
    ("2002:a9fe:a9fe::1", "PRIVATE", True), ("2002:7f00:1::1", "PUBLIC", True),
])
def test_ip_block_reason_matrix(sf, ip, zone, blocked):
    assert bool(sf.ip_block_reason(ip, zone)) is blocked


@pytest.mark.parametrize("lit", ["[::1]", "[::ffff:127.0.0.1]", "[fe80::1%25eth0]", "[fe80::1%eth0]", "[2130706433]",
                                 "[::1", "::1"])
def test_ipv6_literal_forms(sf, lit):
    # the normaliser either canonicalises a valid literal or refuses a malformed one; loopback is then refused
    # by the zone check unless the operator explicitly allowlisted it as PRIVATE
    try:
        canon = sf.normalize_host(lit)
    except sf.SSRFBlocked:
        return
    pol = sf.FetchPolicy.of([sf.HostRule(canon, "PUBLIC", frozenset({80}))])
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch(f"http://[{canon}]/x", policy=pol)


def test_allowlisted_private_ip_literal_is_reachable(sf, server):
    srv = server(lambda r: (200, {}, b"hello"))
    pol = sf.FetchPolicy.of([sf.HostRule("127.0.0.1", "PRIVATE", frozenset({srv.port}))])
    assert sf.safe_fetch(f"http://127.0.0.1:{srv.port}/p", policy=pol).body == b"hello"


# ── resolve-then-connect, rebinding ──────────────────────────────────────────────────────────────────────────
def test_public_host_resolving_to_loopback_is_blocked(sf):
    with pytest.raises(sf.SSRFBlocked, match="127.0.0.1"):
        sf.safe_fetch("https://editor.test/x", policy=policy(sf, zone="PUBLIC", port=443), resolver=res_to("127.0.0.1"))


def test_any_bad_address_among_several_blocks_the_request(sf):
    with pytest.raises(sf.SSRFBlocked, match="169.254.1.1"):
        sf.safe_fetch("https://editor.test/x", policy=policy(sf, zone="PUBLIC", port=443),
                      resolver=lambda h, p: ["93.184.216.34", "169.254.1.1"])


def test_resolution_is_done_exactly_once_and_connects_to_the_checked_ip(sf, server):
    srv = server(lambda r: (200, {}, b"first-answer"))
    answers = iter([["127.0.0.1"], ["169.254.1.1"]])         # 2nd answer is a different, blocked address
    calls = []

    def resolver(host, port):
        calls.append(host)
        return next(answers)
    r = sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port), resolver=resolver)
    assert r.body == b"first-answer" and r.ip == "127.0.0.1"
    assert calls == ["editor.test"]                              # a second lookup never happens


def test_resolution_change_to_blocked_address_on_first_lookup_is_blocked(sf):
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch("http://editor.test/x", policy=policy(sf), resolver=res_to("169.254.1.1"))


def test_unresolvable_host_is_a_typed_unavailable_not_sent(sf):
    def boom(h, p):
        raise socket.gaierror("no such host")
    with pytest.raises(sf.FetchUnavailable) as ei:
        sf.safe_fetch("http://editor.test/x", policy=policy(sf), resolver=boom)
    assert ei.value.request_sent is False


# ── redirects ────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("loc", ["http://169.254.1.1/internal/path", "http://editor.test/other",
                                 "http://other.test/", "file:///tmp/outside.txt", "/relative"])
def test_redirects_are_never_followed(sf, server, status, loc):
    hits = server(lambda r: (200, {}, b"SHOULD NOT BE REQUESTED"))
    srv = server(lambda r: (status, {"Location": loc}, b""))
    with pytest.raises(sf.SSRFBlocked, match="redirect") as ei:
        sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port), resolver=res_to("127.0.0.1"))
    assert ei.value.extra["location"] == loc               # reported, never requested
    assert len(srv.requests) == 1 and hits.requests == []


def test_redirect_to_blocked_ip_is_not_connected_to(sf, server):
    """Even if the target were reachable, the redirect target is never resolved or contacted."""
    contacted = []
    srv = server(lambda r: (302, {"Location": "http://169.254.1.1/internal/path"}, b""))

    def resolver(host, port):
        contacted.append(host)
        return ["127.0.0.1"]
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port), resolver=resolver)
    assert contacted == ["editor.test"]


# ── limits ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_response_size_limit_declared_and_streamed(sf, server):
    big = server(lambda r: (200, {}, b"x" * 5000))
    with pytest.raises(sf.FetchTooLarge):
        sf.safe_fetch(f"http://editor.test:{big.port}/x", policy=policy(sf, port=big.port, max_bytes=1000),
                      resolver=res_to("127.0.0.1"))
    # undeclared length (chunked / no Content-Length): still enforced while streaming
    import http.server, socketserver, threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"y" * 100)
            except OSError:
                pass

        def log_message(self, *a):
            pass
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with pytest.raises(sf.FetchTooLarge):
            sf.safe_fetch(f"http://editor.test:{httpd.server_address[1]}/x",
                          policy=policy(sf, port=httpd.server_address[1], max_bytes=1000), resolver=res_to("127.0.0.1"))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_request_body_size_limit(sf):
    with pytest.raises(sf.SSRFBlocked, match="too large"):
        sf.safe_fetch("http://editor.test/x", policy=policy(sf, max_request_bytes=10), method="POST", body=b"x" * 11,
                      resolver=res_to("127.0.0.1"))


def test_timeout_is_a_typed_unavailable_with_request_sent(sf, server):
    import time as _t
    srv = server(lambda r: (_t.sleep(1.5) or (200, {}, b"late")))
    with pytest.raises(sf.FetchUnavailable) as ei:
        sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port, timeout_s=0.3),
                      resolver=res_to("127.0.0.1"))
    assert ei.value.request_sent is True


def test_connection_refused_is_not_sent(sf):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(sf.FetchUnavailable) as ei:
        sf.safe_fetch(f"http://editor.test:{port}/x", policy=policy(sf, port=port), resolver=res_to("127.0.0.1"))
    assert ei.value.request_sent is False


def test_content_encoding_is_refused(sf, server):
    srv = server(lambda r: (200, {"Content-Encoding": "gzip"}, b"\x1f\x8b"))
    with pytest.raises(sf.BackendError, match="Content-Encoding"):
        sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port), resolver=res_to("127.0.0.1"))


# ── headers / methods ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hdr", ["Cookie", "Host", "X-Forwarded-For", "Proxy-Authorization", "Transfer-Encoding",
                                 "X-Anything"])
def test_request_header_allowlist(sf, hdr):
    with pytest.raises(sf.SSRFBlocked, match="header"):
        sf.safe_fetch("http://editor.test/x", policy=policy(sf), headers={hdr: "v"}, resolver=res_to("127.0.0.1"))


def test_header_value_injection_is_refused(sf):
    with pytest.raises(sf.SSRFBlocked):
        sf.safe_fetch("http://editor.test/x", policy=policy(sf), headers={"Accept": "a\r\nX-Evil: 1"},
                      resolver=res_to("127.0.0.1"))


@pytest.mark.parametrize("m", ["PUT", "DELETE", "PATCH", "CONNECT", "TRACE"])
def test_method_allowlist(sf, m):
    with pytest.raises(sf.SSRFBlocked, match="method"):
        sf.safe_fetch("http://editor.test/x", policy=policy(sf), method=m, resolver=res_to("127.0.0.1"))


def test_allowed_headers_pass_and_response_headers_are_reduced(sf, server):
    srv = server(lambda r: (200, {"Content-Type": "text/plain", "Set-Cookie": "a=b", "X-Secret": "s"}, b"ok"))
    r = sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port),
                      headers={"Accept": "*/*", "Authorization": "Bearer t"}, resolver=res_to("127.0.0.1"))
    assert srv.requests[0].headers["Authorization"] == "Bearer t"
    assert "set-cookie" not in r.headers and "x-secret" not in r.headers and r.headers["content-type"] == "text/plain"
    assert srv.requests[0].headers.get("Accept-Encoding") == "identity"


def test_proxy_environment_is_ignored(sf, server, monkeypatch):
    srv = server(lambda r: (200, {}, b"direct"))
    for v in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(v, "http://127.0.0.1:9")
    assert sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port),
                         resolver=res_to("127.0.0.1")).body == b"direct"


def test_post_body_roundtrip(sf, server):
    srv = server(lambda r: (200, {}, r.body[::-1]))
    r = sf.safe_fetch(f"http://editor.test:{srv.port}/x", policy=policy(sf, port=srv.port), method="POST", body=b"abc",
                      headers={"Content-Type": "application/json"}, resolver=res_to("127.0.0.1"))
    assert r.body == b"cba"


# ── TLS: pinned IP + hostname verification ───────────────────────────────────────────────────────────────────
def _tls_material(tmp_path, cn):
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / f"{cn}.crt", tmp_path / f"{cn}.key"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
    return cp, kp


def _tls_server(tmp_path, cn):
    import http.server, socketserver, threading
    cp, kp = _tls_material(tmp_path, cn)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"secure")

        def log_message(self, *a):
            pass
    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(cp), str(kp))
    httpd.socket = sctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, cp


def test_https_verifies_the_hostname_against_the_pinned_connection(sf, tmp_path):
    httpd, cp = _tls_server(tmp_path, "editor.test")
    try:
        port = httpd.server_address[1]
        ctx = ssl.create_default_context(cafile=str(cp))
        pol = policy(sf, zone="PRIVATE", port=port)
        r = sf.safe_fetch(f"https://editor.test:{port}/x", policy=pol, resolver=res_to("127.0.0.1"), ssl_context=ctx)
        assert r.body == b"secure"
        # a certificate for another name must NOT be accepted even though the TCP target is the same pinned IP
        other = sf.FetchPolicy.of([sf.HostRule("other.test", "PRIVATE", frozenset({port}))])
        with pytest.raises(sf.FetchUnavailable):
            sf.safe_fetch(f"https://other.test:{port}/x", policy=other, resolver=res_to("127.0.0.1"), ssl_context=ctx)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_https_untrusted_certificate_fails_closed(sf, tmp_path):
    httpd, _ = _tls_server(tmp_path, "editor.test")
    try:
        port = httpd.server_address[1]
        with pytest.raises(sf.FetchUnavailable):
            sf.safe_fetch(f"https://editor.test:{port}/x", policy=policy(sf, zone="PRIVATE", port=port),
                          resolver=res_to("127.0.0.1"))               # default trust store: self-signed => refused
    finally:
        httpd.shutdown()
        httpd.server_close()
