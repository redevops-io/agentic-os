from __future__ import annotations

import importlib
import time

findings_mod = importlib.import_module("deploy-scan.findings")
jobs = importlib.import_module("deploy-scan.jobs")

Finding = findings_mod.Finding


def _wait_until_done(job, timeout=5.0):
    deadline = time.time() + timeout
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.01)
    return job


def test_a_scanner_that_returns_findings_lands_in_results(monkeypatch):
    monkeypatch.setitem(jobs.SCANNERS, "fake", type("M", (), {"run": staticmethod(lambda t: [
        Finding(severity="high", name="n", target=t)])}))
    store = jobs.JobStore()
    job = _wait_until_done(store.start("fake", "some-target"))
    assert job.state == "done"
    results = job.to_results()
    assert results["summary"] == {"target": "some-target", "total": 1, "by_severity": {"high": 1}, "max_severity": "high"}
    assert results["findings"][0]["name"] == "n"


def test_a_scanner_that_raises_becomes_an_error_job_not_a_crash(monkeypatch):
    def _boom(_target):
        raise RuntimeError("scanner exploded")
    monkeypatch.setitem(jobs.SCANNERS, "fake", type("M", (), {"run": staticmethod(_boom)}))
    store = jobs.JobStore()
    job = _wait_until_done(store.start("fake", "t"))
    assert job.state == "error"
    assert "scanner exploded" in job.error
    assert job.to_results()["error"] == job.error


def test_an_unknown_job_id_is_none():
    store = jobs.JobStore()
    assert store.get("does-not-exist") is None


def test_status_before_completion_reports_running():
    store = jobs.JobStore()
    import threading
    import unittest.mock as mock
    gate = threading.Event()
    with mock.patch.dict(jobs.SCANNERS, {"slow": type("M", (), {"run": staticmethod(lambda t: (gate.wait(2), [])[1])})}):
        job = store.start("slow", "t")
        assert job.to_status()["state"] == "running"
        gate.set()
        _wait_until_done(job)
        assert job.state == "done"
