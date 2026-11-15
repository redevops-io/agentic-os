"""The real integration proof: a live deploy-scan service (apps/deploy-scan) answering the REAL
deploy_scan.py client (this module's own `scan`/`status`/`results`), over real HTTP, end to end.

No contract was assumed — the service in apps/deploy-scan was built to match exactly what this client
already sends (see its own module docstring), and this test is what confirms that the match is real,
not just read correctly from the source twice.
"""
from __future__ import annotations

import importlib
import threading
import time

deploy_scan = importlib.import_module("edge-sentinel.deploy_scan")
server = importlib.import_module("deploy-scan.server")
jobs = importlib.import_module("deploy-scan.jobs")
findings_mod = importlib.import_module("deploy-scan.findings")

Finding = findings_mod.Finding


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_scan_status_results_round_trip_against_a_real_running_deploy_scan_service(monkeypatch):
    monkeypatch.setitem(jobs.SCANNERS, "fake-profile", type("M", (), {"run": staticmethod(lambda t: [
        Finding(severity="high", name="a fake but structurally real finding", target=t, ref="REF-1")])}))
    monkeypatch.setattr(server, "_STORE", jobs.JobStore())
    monkeypatch.setenv("SCAN_ALLOWLIST", "fake-profile=example-target")

    port = _free_port()
    httpd = server.serve("127.0.0.1", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(deploy_scan, "DEPLOY_SCAN_MCP_URL", f"http://127.0.0.1:{port}/")

        started = deploy_scan.scan({"target": "example-target", "profile": "fake-profile"})
        assert "Started a fake-profile scan" in started["text"], started
        job_id = started["data"]["job_id"]

        deadline = time.time() + 5
        state = deploy_scan.status({"job_id": job_id})
        while state["data"].get("state") == "running" and time.time() < deadline:
            time.sleep(0.02)
            state = deploy_scan.status({"job_id": job_id})
        assert state["data"]["state"] == "done", state

        results = deploy_scan.results({"job_id": job_id})
        assert "1 finding(s)" in results["text"] and "high: 1" in results["text"], results
        assert results["data"]["findings"][0]["ref"] == "REF-1"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_scan_refuses_a_target_the_deploy_scan_service_has_not_allowlisted(monkeypatch):
    monkeypatch.setattr(server, "_STORE", jobs.JobStore())
    monkeypatch.setenv("SCAN_ALLOWLIST", "")  # nothing allowlisted

    port = _free_port()
    httpd = server.serve("127.0.0.1", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(deploy_scan, "DEPLOY_SCAN_MCP_URL", f"http://127.0.0.1:{port}/")
        out = deploy_scan.scan({"target": "not-allowlisted", "profile": "web-baseline"})
        assert "Couldn't start the scan" in out["text"] and "SCAN_ALLOWLIST" in out["data"]["error"], out
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_scan_reports_unavailable_with_no_url_configured(monkeypatch):
    monkeypatch.setattr(deploy_scan, "DEPLOY_SCAN_MCP_URL", "")
    out = deploy_scan.scan({"target": "x"})
    assert "DEPLOY_SCAN_MCP_URL unset" in out["data"]["error"]


def test_scan_without_a_target_asks_for_one_without_calling_the_service(monkeypatch):
    called = []
    monkeypatch.setattr(deploy_scan, "_call", lambda *a, **k: called.append(1))
    out = deploy_scan.scan({})
    assert "Give me a target" in out["text"]
    assert called == []
