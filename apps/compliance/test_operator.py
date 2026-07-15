"""agentic-compliance as a Mission Runtime operator — proof over a fixture SCAP report.

Exercises the REAL core logic (XCCDF parsing → KPIs, explain, remediate) and the SDK-mount
contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's own
HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

The "core" here is a local OpenSCAP results file rather than an HTTP service, so instead of
a fake httpx we point core.SCAP_RESULTS at a fixture XCCDF file (and core.SCAP_SCAN_SCRIPT
at a nonexistent path, so compliance.scan skips the long-running subprocess and just
re-reads the fixture — the real scan() code path, minus the shell-out).

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/compliance/test_operator.py -q
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance import core
from compliance.operator import build_compliance_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fixture OpenSCAP XCCDF results file (2 failing + 1 passing control) ─────
SSH_RULE = "xccdf_org.ssgproject.content_rule_ssh_disable_root_login"
UMASK_RULE = "xccdf_org.ssgproject.content_rule_accounts_umask_etc_profile"
TELNET_RULE = "xccdf_org.ssgproject.content_rule_package_telnet_removed"

FIXTURE_XCCDF = f"""<?xml version="1.0" encoding="UTF-8"?>
<Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2"
           id="xccdf_org.ssgproject.content_benchmark_CIS">
  <Rule id="{SSH_RULE}" severity="high">
    <title>Disable SSH root login</title>
    <description>Ensure the SSH daemon does not permit direct root login.</description>
    <rationale>Direct root login over SSH lets an attacker brute-force a well-known account.</rationale>
    <fix>Set PermitRootLogin no in /etc/ssh/sshd_config and restart sshd.</fix>
  </Rule>
  <Rule id="{UMASK_RULE}" severity="medium">
    <title>Set default umask in /etc/profile</title>
    <description>Ensure a restrictive default umask is set for interactive shells.</description>
    <rationale>A permissive umask creates world-readable files by default.</rationale>
    <fix>Add umask 027 to /etc/profile.</fix>
  </Rule>
  <Rule id="{TELNET_RULE}" severity="low">
    <title>Remove telnet package</title>
    <description>Ensure the telnet client package is not installed.</description>
    <rationale>Telnet transmits credentials in cleartext.</rationale>
    <fix>apt purge telnet</fix>
  </Rule>
  <TestResult id="xccdf_org.ssgproject.content_testresult_default">
    <rule-result idref="{SSH_RULE}" severity="high"><result>fail</result></rule-result>
    <rule-result idref="{UMASK_RULE}" severity="medium"><result>fail</result></rule-result>
    <rule-result idref="{TELNET_RULE}" severity="low"><result>pass</result></rule-result>
  </TestResult>
</Benchmark>
"""


@pytest.fixture(autouse=True)
def _fixture_scap(monkeypatch, tmp_path):
    results = tmp_path / "scan-results.xml"
    results.write_text(FIXTURE_XCCDF)
    # No cache bleed between tests, and the scan script is absent so scan() skips the shell-out.
    core._CACHE.update(ts=0.0, data=None)
    core._PARSE_CACHE.update(mtime=None, rules=None, results=None)
    monkeypatch.setattr(core, "SCAP_RESULTS", results)
    monkeypatch.setattr(core, "SCAP_SCAN_SCRIPT", tmp_path / "nope-scan.sh")
    # Point the append-only consent evidence ledger at a tmp file (starts absent).
    monkeypatch.setattr(core, "CONSENT_LEDGER", tmp_path / "evidence" / "consent-ledger.jsonl")
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_compliance_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {
        "compliance.scan", "compliance.explain", "compliance.remediate",
        "compliance.file_consent",
    }
    # applying a host fix is gated (matches modules.yaml policy_change gate)
    assert caps["compliance.remediate"]["approval_required"] is True
    assert caps["compliance.remediate"]["side_effecting"] is True
    # filing a consent record is the onboarding human gate — gated + side-effecting
    assert caps["compliance.file_consent"]["approval_required"] is True
    assert caps["compliance.file_consent"]["side_effecting"] is True
    assert "consent_filed" in caps["compliance.file_consent"]["provides"]
    # read-only capabilities are not gated
    assert caps["compliance.scan"]["approval_required"] is False
    assert caps["compliance.explain"]["approval_required"] is False
    assert "control_explanation" in caps["compliance.explain"]["provides"]


def test_invoke_scan_parses_real_fixture(client):
    res = client.post("/invoke", json={"capability": "compliance.scan", "inputs": {}}).json()["result"]
    # the REAL core parsed the fixture XCCDF: 1 pass / 2 fail across 3 scored controls
    assert res["status"] == "done"
    assert res["passing"] == 1 and res["failing"] == 2
    assert res["pass_rate"] == "33%"
    assert res["rc"] is None  # scan script absent → no shell-out, just re-read the fixture


def test_invoke_explain_reads_scap_content(client):
    body = {"capability": "compliance.explain", "inputs": {"rule_id": "ssh_disable_root_login"}}
    res = client.post("/invoke", json=body).json()["result"]
    assert res["status"] == "done"
    assert res["rule_id"] == SSH_RULE
    assert res["severity"] == "high"
    # remediation + explanation are built from the fixture's OWN description/rationale/fix
    assert "PermitRootLogin no" in res["remediation"]
    assert "Disable SSH root login" in res["explanation"]
    assert res["source"] == "openscap-content"


def test_invoke_remediate_is_pending_approval(client):
    body = {"capability": "compliance.remediate", "inputs": {"rule_id": UMASK_RULE}}
    res = client.post("/invoke", json=body).json()["result"]
    assert res["status"] == "pending_approval"
    assert res["approval"] == "policy_change"
    assert res["rule_id"] == UMASK_RULE
    assert "umask 027" in res["proposed_remediation"]


def test_idempotency_dedupes_invocation(client):
    body = {"capability": "compliance.remediate", "inputs": {"rule_id": SSH_RULE},
            "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second and first["status"] == "pending_approval"


def test_invoke_file_consent_appends_to_ledger(client):
    body = {"capability": "compliance.file_consent",
            "inputs": {"customer": "Summit Roofing Co.", "subscription": "pro-annual"}}
    res = client.post("/invoke", json=body).json()["result"]
    assert res["status"] == "done"
    assert res["action"] == "file_consent"
    assert res["consent_filed"] is True
    assert res["consent_id"].startswith("consent-")

    # exactly one consent line landed in the append-only JSONL evidence ledger
    lines = [l for l in core.CONSENT_LEDGER.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec == {
        "kind": "consent",
        "customer": "Summit Roofing Co.",
        "subscription": "pro-annual",
        "consent_id": res["consent_id"],
        "filed": True,
    }


def test_file_consent_is_idempotent(client):
    """Re-filing the same customer/subscription dedupes — the ledger keeps ONE record."""
    body = {"capability": "compliance.file_consent",
            "inputs": {"customer": "Summit Roofing Co.", "subscription": "pro-annual"}}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    # deterministic consent_id → the second file is a no-op append
    assert first["consent_id"] == second["consent_id"]
    assert second["consent_filed"] is True
    lines = [l for l in core.CONSENT_LEDGER.read_text().splitlines() if l.strip()]
    assert len(lines) == 1  # deduped, not appended twice

    # a DIFFERENT subscription is a distinct consent record → a second line appends
    other = {"capability": "compliance.file_consent",
             "inputs": {"customer": "Summit Roofing Co.", "subscription": "enterprise"}}
    res = client.post("/invoke", json=other).json()["result"]
    assert res["consent_id"] != first["consent_id"]
    lines = [l for l in core.CONSENT_LEDGER.read_text().splitlines() if l.strip()]
    assert len(lines) == 2


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"compliance": "http://compliance"}, transport=_transport)
    scan = oc.invoke("compliance", "compliance.scan", {}, idempotency_key="m-1")
    assert scan["status"] == "done" and scan["failing"] == 2

    rem = oc.invoke("compliance", "compliance.remediate", {"rule_id": SSH_RULE}, idempotency_key="m-2")
    assert rem["status"] == "pending_approval" and rem["approval"] == "policy_change"
