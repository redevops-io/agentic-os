"""Declared-output and declaration contracts for the compliance operator."""
from __future__ import annotations

import threading
import types

import pytest

from agentic_os.mission.compensation import Reversibility, lint_capability, registry_problems, reversibility_of
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.verify import CompositeVerifier
from compliance import core
from compliance.operator import build_compliance_operator

APPR = {"_approval": {"approved": True}}
RULE = "xccdf_org.ssgproject.content_rule_ssh_disable_root_login"


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload or {}, ""

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "CONSENT_LEDGER", tmp_path / "ev" / "ledger.jsonl")
    monkeypatch.setattr(core, "SCAP_SCAN_SCRIPT", tmp_path / "none.sh")
    monkeypatch.setattr(core, "_resolve_rule", lambda rid: {"id": RULE, "title": "t", "severity": "high", "fix": "f"})
    op = build_compliance_operator()
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    return op, reg, CompositeVerifier(reg, lambda m: {"*"})


def _verify(rig, cap, result):
    node = types.SimpleNamespace(capability=cap, side_effecting=True, assertions=[])
    return rig[2].verify(node, result, None, None)


def test_remediate_with_connector_passes_verifier(rig, monkeypatch):
    opened = []
    monkeypatch.setenv("COMPLIANCE_TICKETING_URL", "http://tickets.invalid/t")
    monkeypatch.setattr(core, "_open_ticket", lambda url, t: (opened.append(t) or "T-1", True))
    res = rig[0].invoke("compliance.remediate", {"rule_id": RULE, **APPR})
    assert res["status"] == "done" and _verify(rig, "compliance.remediate", res).decision.name == "ACCEPT"
    assert len(opened) == 1


def test_remediate_without_connector_records_recommendation_only(rig, monkeypatch):
    monkeypatch.delenv("COMPLIANCE_TICKETING_URL", raising=False)
    res = rig[0].invoke("compliance.remediate", {"rule_id": RULE, **APPR})
    assert res["ticket_opened"] is False and res["remediation_staged"] is True


def test_file_consent_result_passes_verifier(rig):
    res = rig[0].invoke("compliance.file_consent", {"customer": "acme", "subscription": "pro", **APPR})
    assert res["status"] == "done" and _verify(rig, "compliance.file_consent", res).decision.name == "ACCEPT"


def test_scan_is_single_flight(rig, monkeypatch, tmp_path):
    started, release, results = threading.Event(), threading.Event(), []

    def slow(*a, **k):
        started.set()
        release.wait(5)
        return types.SimpleNamespace(returncode=0, stderr="")
    script = tmp_path / "scan.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(core, "SCAP_SCAN_SCRIPT", script)
    monkeypatch.setattr(core.subprocess, "run", slow)
    monkeypatch.setattr(core, "fetch_activity", lambda force=False: {
        "counts": {"pass": 1, "fail": 0, "notapplicable": 0, "scored": 1}, "kpis": [{"value": "100%"}]})
    monkeypatch.setattr(core, "scan_target", lambda: {}, raising=False)
    monkeypatch.setattr(core, "_ssg_verification", lambda t: {}, raising=False)
    t = threading.Thread(target=lambda: results.append(core.scan()))
    t.start()
    assert started.wait(5)
    second = core.scan()
    release.set()
    t.join(5)
    assert second["status"] == "error" and "already" in second["error"]


def test_capability_declarations_pass_lint(rig):
    _, reg, _ = rig
    caps = reg.all()
    assert [lint_capability(c) for c in caps if lint_capability(c)] == []
    assert registry_problems(caps, reg.get, require_verify=False) == []
    by = {c.name: c for c in caps}
    # remediation and consent filings are never auto-reversed
    for name in ("compliance.remediate", "compliance.file_consent"):
        assert reversibility_of(by[name]) is Reversibility.IRREVERSIBLE and not by[name].undo
