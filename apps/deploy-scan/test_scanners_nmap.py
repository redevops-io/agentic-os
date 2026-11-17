"""Real nmap, against a loopback HTTP server this test starts itself — never a third-party host."""
from __future__ import annotations

import importlib
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

nmap_web = importlib.import_module("deploy-scan.scanners.nmap_web")

pytestmark = pytest.mark.skipif(not shutil.which("nmap"), reason="nmap is not installed")


class _Echo(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture()
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_finds_the_one_open_port_we_actually_opened(http_server):
    port = http_server.server_address[1]
    results = nmap_web.run(f"127.0.0.1:{port}")
    assert any(f.target == f"127.0.0.1:{port}/tcp" for f in results), [r.target for r in results]


def test_a_closed_port_produces_no_finding():
    # port 1 is reserved/closed on essentially every host and is in no SCAN_ALLOWLIST this test writes to
    results = nmap_web.run("127.0.0.1:1")
    assert results == []


def test_a_bare_host_scans_the_documented_default_port_set():
    host, ports = nmap_web._parse_target("127.0.0.1")
    assert host == "127.0.0.1"
    assert ports == nmap_web._DEFAULT_PORTS


def test_unavailable_when_nmap_is_missing(monkeypatch):
    monkeypatch.setattr(nmap_web.shutil, "which", lambda _: None)
    results = nmap_web.run("127.0.0.1:1")
    assert len(results) == 1 and results[0].severity == "info" and "not installed" in results[0].name
