"""ProductClient over a real socket: credentials, no redirects, size cap, typed errors, nothing sensitive leaks."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentic_os.products.client import MAX_BYTES, ProductClient, ProductError
from agentic_os.products.registry import load_registry
from tests._products_fakes import DECISION_TOKEN, SERVICE_TOKEN, FakeSentinel, write_registry


@pytest.fixture
def fake():
    f = FakeSentinel()
    base = f.serve()
    yield f, base
    f.close()


def _client(tmp_path, base):
    return ProductClient(load_registry(write_registry(tmp_path, base)).get("edge-sentinel"))


def test_reads_use_the_service_token_and_the_acting_user_claim_but_never_an_authorization_header(fake, tmp_path):
    f, base = fake
    c = _client(tmp_path, base)
    out = c.get("/api/v1/posture", actor="projects:local-owner", source="sidekick")
    assert out["decisions_active"] == 2
    h = f.calls[-1]["headers"]
    assert h["X-Control-Token"] == SERVICE_TOKEN and h["X-Acting-User"] == "projects:local-owner" and h["X-Acting-Source"] == "sidekick"
    assert "Authorization" not in h and "Cookie" not in h


def test_the_decision_token_is_the_second_of_the_pair(fake, tmp_path):
    f, base = fake
    c = _client(tmp_path, base)
    with pytest.raises(ProductError) as e:
        c.post("/api/v1/cases/case-0123456789abcdef/actions/act-0123456789abcdef/decision", {"approved": True}, auth="service")
    assert e.value.code == "unauthenticated" and e.value.status == 401          # the service token cannot carry a decision
    assert c.decision_token() == DECISION_TOKEN


def test_probe_needs_no_credential_and_reports_not_ready(fake, tmp_path):
    f, base = fake
    c = _client(tmp_path, base)
    assert c.probe()["ok"] is True
    assert "X-Control-Token" not in f.calls[-1]["headers"]
    f.ready = False
    assert c.probe()["ok"] is False


def test_unreachable_and_missing_token_are_typed_unavailable_errors(tmp_path):
    c = ProductClient(load_registry(write_registry(tmp_path, "http://127.0.0.1:9")).get("edge-sentinel"))
    with pytest.raises(ProductError) as e:
        c.get("/api/v1/posture")
    assert e.value.unavailable and e.value.code == "unreachable"
    c2 = ProductClient(load_registry(write_registry(tmp_path, "http://127.0.0.1:9", with_tokens=False)).get("edge-sentinel"))
    with pytest.raises(ProductError) as e2:
        c2.get("/api/v1/posture")
    assert e2.value.code == "no_token" and not c2.configured


def test_paths_are_product_relative_only(fake, tmp_path):
    _, base = fake
    c = _client(tmp_path, base)
    for bad in ("http://evil.example/x", "api/v1/x", "/api/../etc", "//evil/x"):
        with pytest.raises(ProductError):
            c.get(bad)


class _Evil(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.end_headers()
        elif self.path == "/big":
            raw = b'{"x":"' + b"a" * (MAX_BYTES + 10) + b'"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/html":
            raw = b"<html>no</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/err":
            raw = json.dumps({"error": "conflict", "detail": "d", "reason": "r", "remedy": "m"}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            raw = b'{"boom":1}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)


def test_redirects_oversize_non_json_and_5xx_are_refused_or_typed(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Evil)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = ProductClient(load_registry(write_registry(tmp_path, f"http://127.0.0.1:{srv.server_address[1]}")).get("edge-sentinel"))
        for path, code in (("/redirect", "bad_response"), ("/big", "bad_response"), ("/html", "bad_response")):
            with pytest.raises(ProductError) as e:
                c.get(path)
            assert e.value.code == code, path
        with pytest.raises(ProductError) as e:
            c.get("/err")
        assert (e.value.code, e.value.status, e.value.reason, e.value.remedy) == ("conflict", 409, "r", "m")
        with pytest.raises(ProductError) as e:
            c.get("/boom")
        assert e.value.unavailable and e.value.status == 500
    finally:
        srv.shutdown()
        srv.server_close()


def test_errors_never_contain_the_token(fake, tmp_path):
    f, base = fake
    c = _client(tmp_path, base)
    try:
        c.get("/api/v1/actions/act-ffffffffffffffff")
    except ProductError as e:
        assert SERVICE_TOKEN not in repr(e) and SERVICE_TOKEN not in json.dumps(e.as_dict())
