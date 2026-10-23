"""Assurance sensors: what an operator should look at before a customer does (audit D / Slice 2)."""
import time

import pytest

from agentic_os.mission.assurance import assurance_findings
from agentic_os.mission.store import MemoryEventStore


def _log(*items, t0=1000.0):
    s = MemoryEventStore()
    for i, (typ, mid, payload) in enumerate(items):
        s.append(typ, mid, payload)
    for e in s.all():
        e.ts = t0                              # not part of the chain test here; set a fixed time
    return s


def _kinds(findings):
    return sorted(f.kind for f in findings)


def test_a_healthy_mission_has_no_findings():
    s = MemoryEventStore()
    s.append("NodeParked", "m", {"node_id": "n"})
    s.append("ApprovalGranted", "m", {"node_id": "n"})
    s.append("NodeDispatched", "m", {"node_id": "n"})
    s.append("VerificationRecorded", "m", {"node_id": "n"})
    s.append("NodeSucceeded", "m", {"node_id": "n", "capability": "billing.dunning"})
    now = time.time() + 10
    assert assurance_findings(s.all(), now=now, side_effecting={"billing.dunning"}) == []


def test_a_gate_waiting_too_long_is_flagged():
    s = MemoryEventStore()
    s.append("NodeParked", "m", {"node_id": "n"})
    f = assurance_findings(s.all(), now=time.time() + 90_000)
    assert _kinds(f) == ["stuck_gate"]


def test_dispatch_with_no_result_is_flagged_as_possibly_executed():
    s = MemoryEventStore()
    s.append("NodeDispatched", "m", {"node_id": "n"})
    f = assurance_findings(s.all(), now=time.time() + 2_000)
    assert _kinds(f) == ["dispatch_without_result"] and "reconcile" in f[0].detail and f[0].severity == "high"


def test_a_side_effect_without_a_recorded_verification_is_flagged():
    s = MemoryEventStore()
    s.append("NodeSucceeded", "m", {"node_id": "n", "capability": "social.publish"})
    f = assurance_findings(s.all(), now=time.time(), side_effecting={"social.publish"})
    assert _kinds(f) == ["unverified_success"]
    assert assurance_findings(s.all(), now=time.time(), side_effecting={"crm.read"}) == []


def test_repeated_failures_of_one_capability_are_flagged():
    s = MemoryEventStore()
    for i in range(3):
        s.append("NodeFailed", "m", {"node_id": f"n{i}", "capability": "erpnext.write"})
    assert _kinds(assurance_findings(s.all(), now=time.time())) == ["repeated_failures"]


def test_invalidated_and_expired_approvals_are_surfaced():
    s = MemoryEventStore()
    s.append("ApprovalInvalidated", "m", {"node_id": "n", "reason": "inputs changed since approval"})
    s.append("ApprovalExpired", "m", {"node_id": "n", "ttl_s": 60})
    assert _kinds(assurance_findings(s.all(), now=time.time())) == ["approval_expired", "approval_invalidated"]


def test_a_tampered_log_is_critical():
    s = MemoryEventStore()
    s.append("MissionCreated", "m", {"goal": "pay"})
    s.append("NodeSucceeded", "m", {"node_id": "n", "capability": "x", "result": {"amount": 100}})
    s.all()[1].payload["result"]["amount"] = 1                   # edit history in place
    f = [x for x in assurance_findings(s.all(), now=time.time()) if x.kind == "ledger_tampered"]
    assert f and f[0].severity == "critical"


def test_gate_expiry_is_enforced_when_a_ttl_is_configured(monkeypatch):
    from tests.test_mission_inputs_delivery_and_explicit_planning import _op, _rt, _req
    from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher
    from agentic_os.mission.runtime import ApprovalError
    from agentic_os.mission.types import MissionState
    rt = _rt(_op())
    mid = MissionRuntimeLauncher(rt).launch(_req())
    rt.run(mid)
    node = rt.repo.pending_human(mid)["node_id"]
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_TTL_S", "60")
    real = time.time
    monkeypatch.setattr("agentic_os.mission.runtime.time.time", lambda: real() + 120)
    with pytest.raises(ApprovalError):
        rt.approve(mid, node, "approve")
    assert any(e.type == "ApprovalExpired" for e in rt.store.for_mission(mid))
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN
    rt.approve(mid, node, "reject")                              # a rejection is always allowed
    monkeypatch.setattr("agentic_os.mission.runtime.time.time", real)


def test_without_a_ttl_an_old_gate_can_still_be_approved(monkeypatch):
    from tests.test_mission_inputs_delivery_and_explicit_planning import _op, _rt, _req
    from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher
    from agentic_os.mission.types import MissionState
    monkeypatch.delenv("AGENTIC_OS_APPROVAL_TTL_S", raising=False)
    rt = _rt(_op())
    mid = MissionRuntimeLauncher(rt).launch(_req())
    rt.run(mid)
    real = time.time
    monkeypatch.setattr("agentic_os.mission.runtime.time.time", lambda: real() + 10_000_000)
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    assert rt._missions[mid].state is MissionState.SUCCEEDED
