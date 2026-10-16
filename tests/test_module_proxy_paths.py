"""Proxied module paths stay under the module's /api/ prefix."""
import http.server
import threading

import pytest
from fastapi import HTTPException


@pytest.fixture()
def cp_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(tmp_path / "home"))
    from agentic_os import control_plane
    return control_plane


@pytest.mark.parametrize("bad", ["../x", "a/../x", "..", ".", "a//b", "/x", "a\\b", "a%2fb", "%2e%2e/x", "a\x00b", "a\nb"])
def test_unsafe_tails_are_rejected(cp_mod, bad):
    with pytest.raises(HTTPException) as e:
        cp_mod._safe_proxy_path(bad)
    assert e.value.status_code == 400


@pytest.mark.parametrize("good", ["", "status", "a/b/c", "report.json"])
def test_plain_tails_pass(cp_mod, good):
    assert cp_mod._safe_proxy_path(good) == good


def test_encoded_dotdot_never_reaches_upstream(cp_mod, monkeypatch):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        do_POST = do_GET

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    name = next(iter(cp_mod.MODULE_SERVICES))
    monkeypatch.setitem(cp_mod.MODULE_SERVICES, name, f"http://127.0.0.1:{srv.server_port}")
    from fastapi.testclient import TestClient
    c = TestClient(cp_mod.app)
    try:
        for p in (f"/m/{name}/api/..%2fother%2fx", f"/m/{name}/api/%2e%2e/other/x", f"/m/{name}/api/../other/x"):
            assert c.post(p, content=b"{}").status_code in (400, 404)
        assert seen == []
        assert c.get(f"/m/{name}/api/status").status_code == 200
        assert seen == ["/api/status"]
    finally:
        srv.shutdown()
