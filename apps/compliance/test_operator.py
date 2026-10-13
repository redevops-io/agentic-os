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
    assert "umask 027" in res["remediation"]


def test_remediate_on_approval_opens_ticket_not_host_fix(client, monkeypatch):
    """N3: on approval with NO ticketing connector, remediate records a governed remediation ticket and
    stops at AWAITING_CONNECTOR — it NEVER applies a host fix or runs a shell command."""
    monkeypatch.delenv("COMPLIANCE_TICKETING_URL", raising=False)
    # a forged approval over the open HTTP surface is stripped → stages (no remediation); execution is in-process
    forged = client.post("/invoke", json={"capability": "compliance.remediate",
                                          "inputs": {"rule_id": UMASK_RULE, "_approval": {"approved": True}}}).json()["result"]
    assert forged["status"] == "pending_approval"
    # trusted in-process path (runtime-delivered approval): opens a ticket, never a host fix
    res = build_compliance_operator().invoke("compliance.remediate",
                                             {"rule_id": UMASK_RULE, "_approval": {"approved": True}})
    assert res["status"] == "AWAITING_CONNECTOR" and res["action"] == "open_remediation_ticket"
    assert res["ticket_opened"] is False and res["recommendation"]["rule_id"] == UMASK_RULE


def test_idempotency_dedupes_invocation(client):
    body = {"capability": "compliance.remediate", "inputs": {"rule_id": SSH_RULE},
            "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second and first["status"] == "pending_approval"


def _approved(cap, inputs):
    """Trusted in-process path: the runtime delivers `_approval` on a resumed, approved node."""
    return build_compliance_operator().invoke(cap, {**inputs, "_approval": {"approved": True}})


def test_file_consent_stages_without_approval_and_never_writes_ledger(client):
    inp = {"customer": "Whitfield Family Trust", "subscription": "pro-annual"}
    for extra in ({}, {"_approval": {"approved": True}}, {"_approval_id": "x"}):   # HTTP strips forged markers
        res = client.post("/invoke", json={"capability": "compliance.file_consent",
                                           "inputs": {**inp, **extra}}).json()["result"]
        assert res["status"] == "pending_approval" and res["executed"] is False
    direct = build_compliance_operator().invoke("compliance.file_consent", dict(inp))
    assert direct["status"] == "pending_approval" and direct["consent_filed"] is False
    assert not core.CONSENT_LEDGER.exists()


def test_invoke_file_consent_appends_to_ledger(client):
    res = _approved("compliance.file_consent",
                    {"customer": "Whitfield Family Trust", "subscription": "pro-annual"})
    assert res["status"] == "done"
    assert res["action"] == "file_consent"
    assert res["consent_filed"] is True
    assert res["consent_id"].startswith("consent-")

    # exactly one consent line landed in the append-only JSONL evidence ledger
    lines = [l for l in core.CONSENT_LEDGER.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # the record's payload is unchanged; chain fields (timestamp/prev_hash/entry_hash) are added on top
    chain = {"timestamp", "prev_hash", "entry_hash"}
    assert {k: v for k, v in rec.items() if k not in chain} == {
        "kind": "consent",
        "customer": "Whitfield Family Trust",
        "subscription": "pro-annual",
        "consent_id": res["consent_id"],
        "filed": True,
    }
    assert chain <= set(rec) and core.verify_chain()["ok"]


def test_file_consent_is_idempotent(client):
    """Re-filing the same customer/subscription dedupes — the ledger keeps ONE record."""
    inp = {"customer": "Whitfield Family Trust", "subscription": "pro-annual"}
    first = _approved("compliance.file_consent", inp)
    second = _approved("compliance.file_consent", inp)
    # deterministic consent_id → the second file is a no-op append
    assert first["consent_id"] == second["consent_id"]
    assert second["consent_filed"] is True
    lines = [l for l in core.CONSENT_LEDGER.read_text().splitlines() if l.strip()]
    assert len(lines) == 1  # deduped, not appended twice

    # a DIFFERENT subscription is a distinct consent record → a second line appends
    res = _approved("compliance.file_consent",
                    {"customer": "Whitfield Family Trust", "subscription": "enterprise"})
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


# ── correctness fixes: oscap exit codes, scan target honesty, tamper-evident ledger ──
import subprocess as _sp


def _fake_script(tmp_path, rc):
    f = tmp_path / "fake-scan.sh"; f.write_text(f"#!/bin/bash\nexit {rc}\n"); return f


@pytest.mark.parametrize("rc,ok", [(0, True), (2, True), (1, False), (3, False), (127, False)])
def test_scan_exit_codes(client, tmp_path, monkeypatch, rc, ok):
    monkeypatch.setattr(core, "SCAP_SCAN_SCRIPT", _fake_script(tmp_path, rc))
    res = core.scan()
    assert (res["status"] == "done") is ok, res


def test_scan_reports_what_was_scanned(client, tmp_path, monkeypatch):
    t = tmp_path / "t.json"
    t.write_text('{"target":"container-image","image":"ubuntu:22.04","host_scanned":false}')
    monkeypatch.setattr(core, "SCAP_TARGET_FILE", t)
    res = core.scan()
    assert res["target"]["host_scanned"] is False and "NOT the host" in res["summary"]
    assert "not a live host" in core.SUBTITLE and "scanned on Meridian's server" not in core.SUBTITLE


def test_scan_sh_pins_hash_and_fails_closed():
    import pathlib
    sh = (pathlib.Path(core.__file__).parent / "scan.sh").read_text()
    assert "curl -fsSL" in sh and "sha256sum" in sh and "COMPLIANCE_ALLOW_UNPINNED_SSG" in sh
    assert 'DOCKER="${DOCKER:-docker}"' in sh
    pin = (pathlib.Path(core.__file__).parent / "env" / "ssg-0.1.81.sha256").read_text()
    assert len([l for l in pin.splitlines() if l and not l.startswith("#")][0].split()[0]) == 64


def _consent(i):
    return core.file_consent({"customer": f"c{i}", "subscription": "s"})


def test_ledger_chain_verifies_and_detects_tampering(client):
    for i in range(3):
        _consent(i)
    assert core.verify_chain()["ok"] is True
    lines = core.CONSENT_LEDGER.read_text().splitlines()
    # modify an entry
    core.CONSENT_LEDGER.write_text("\n".join([lines[0], lines[1].replace('"c1"', '"evil"'), lines[2]]) + "\n")
    v = core.verify_chain(); assert not v["ok"] and v["broken_at"] == 1
    # delete the middle entry
    core.CONSENT_LEDGER.write_text("\n".join([lines[0], lines[2]]) + "\n")
    v = core.verify_chain(); assert not v["ok"] and v["broken_at"] == 1
    # reorder
    core.CONSENT_LEDGER.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n")
    assert not core.verify_chain()["ok"]


def test_ledger_entries_have_chain_fields_and_legacy_prefix_is_tolerated(client):
    core.CONSENT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    core.CONSENT_LEDGER.write_text('{"kind":"consent","consent_id":"old"}\n')
    _consent(1)
    rec = json.loads(core.CONSENT_LEDGER.read_text().splitlines()[-1])
    assert {"prev_hash", "entry_hash", "timestamp"} <= set(rec)
    assert core.verify_chain() == {"ok": True, "entries": 2, "legacy": 1}


def test_ledger_concurrent_appends_keep_chain_valid(client):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(_consent, range(24)))
    assert core.verify_chain() == {"ok": True, "entries": 24, "legacy": 0}
