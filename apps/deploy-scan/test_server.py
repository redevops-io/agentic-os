from __future__ import annotations

import importlib
import time

import pytest

jobs = importlib.import_module("deploy-scan.jobs")
server = importlib.import_module("deploy-scan.server")
findings_mod = importlib.import_module("deploy-scan.findings")

Finding = findings_mod.Finding


def _wait_until_done(job_id, timeout=5.0):
    deadline = time.time() + timeout
    job = server._STORE.get(job_id)
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.01)
    return job


def _rpc(name, arguments):
    return server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch):
    monkeypatch.setattr(server, "_STORE", jobs.JobStore())


def test_scan_start_is_refused_when_the_target_is_not_allowlisted(monkeypatch):
    monkeypatch.setenv("SCAN_ALLOWLIST", "")
    resp = _rpc("scan_start", {"target": "127.0.0.1:1", "profile": "web-baseline"})
    sc = resp["result"]["structuredContent"]
    assert "error" in sc and "not in SCAN_ALLOWLIST" in sc["error"]


def test_scan_start_runs_when_the_target_is_allowlisted(monkeypatch):
    monkeypatch.setenv("SCAN_ALLOWLIST", "web-baseline=127.0.0.1:1")
    resp = _rpc("scan_start", {"target": "127.0.0.1:1", "profile": "web-baseline"})
    sc = resp["result"]["structuredContent"]
    # accepted, not refused. The scan hits a closed port, so it may already have finished when the reply is read.
    assert "job_id" in sc and "error" not in sc and sc["state"] in ("running", "done"), sc
    _wait_until_done(sc["job_id"])


def test_an_unknown_profile_is_refused_before_the_allowlist_is_even_checked(monkeypatch):
    monkeypatch.setenv("SCAN_ALLOWLIST", "nonsense-profile=anything")
    resp = _rpc("scan_start", {"target": "anything", "profile": "nonsense-profile"})
    sc = resp["result"]["structuredContent"]
    assert "unknown profile" in sc["error"]


def test_scan_status_and_scan_results_round_trip_through_the_same_job_id(monkeypatch):
    monkeypatch.setitem(jobs.SCANNERS, "fake", type("M", (), {"run": staticmethod(lambda t: [
        Finding(severity="critical", name="n", target=t)])}))
    monkeypatch.setenv("SCAN_ALLOWLIST", "fake=t")
    job_id = _rpc("scan_start", {"target": "t", "profile": "fake"})["result"]["structuredContent"]["job_id"]
    _wait_until_done(job_id)

    status = _rpc("scan_status", {"job_id": job_id})["result"]["structuredContent"]
    assert status["state"] == "done"

    results = _rpc("scan_results", {"job_id": job_id})["result"]["structuredContent"]
    assert results["summary"]["max_severity"] == "critical"
    assert results["findings"][0]["name"] == "n"


def test_an_unknown_job_id_is_an_error_not_a_crash():
    for tool in ("scan_status", "scan_results"):
        sc = _rpc(tool, {"job_id": "does-not-exist"})["result"]["structuredContent"]
        assert sc["error"] == "no such job_id"


def test_an_unknown_tool_name_is_a_json_rpc_error():
    resp = server.dispatch({"method": "tools/call", "params": {"name": "delete_everything", "arguments": {}}})
    assert resp["error"]["code"] == "unknown_tool"


def test_a_non_tools_call_method_is_refused():
    resp = server.dispatch({"method": "tools/list"})
    assert resp["error"]["code"] == "method_not_found"
