"""End-to-end runtime security telemetry: the Executor emits canonical events at the capability boundary
(not agent-reported), and the trajectory correlates a series of individually-permissible calls into a
disposition — including plan-vs-observed divergence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentic_os.mission.executor import Executor, InMemoryOperatorClient, OperatorError
from agentic_os.mission.security_monitor import SecurityMonitor
from agentic_os.mission.types import Node

pytest.importorskip("runtime_contracts")
from runtime_contracts import GovernanceDisposition   # noqa: E402

# Declared capability surface (from the registry, not the agent): what each capability may reach.
DESCS = {
    "crm.read": SimpleNamespace(data_classifications=("pii",), required_authority=("read:crm",), network=()),
    "report.generate": SimpleNamespace(),
    "storage.upload": SimpleNamespace(data_classifications=(), required_authority=("write:storage",),
                                      network=("s3.external.com",)),
}


def _executor(planned=(), *, upload_raises=False):
    handlers = {"crm.read": lambda i: {"records": 2000},
                "report.generate": lambda i: {},
                "storage.upload": (lambda i: (_ for _ in ()).throw(OperatorError("blocked")))
                if upload_raises else (lambda i: {})}
    mon = SecurityMonitor(mission_id="m1", planned_capabilities=planned,
                          descriptor_for=DESCS.get)
    return Executor(InMemoryOperatorClient(handlers), monitor=mon), mon


def test_boundary_emits_events_the_operator_never_reported():
    ex, mon = _executor()
    ex.run(Node(capability="crm.read", operator="op"), {})
    # the operator handler returned {"records": 2000} and reported no security facts; the EXECUTOR produced
    # the event, and 'pii' came from the descriptor — a compromised operator could not have hidden it.
    assert len(mon.trajectory.events) == 1
    assert mon.trajectory.events[0].capability == "crm.read"
    assert "pii" in mon.trajectory.data_classifications


def test_series_of_allows_correlates_to_deny():
    ex, mon = _executor()
    ex.run(Node(capability="crm.read", operator="op"), {})        # 2000 pii records
    ex.run(Node(capability="report.generate", operator="op"), {})
    ex.run(Node(capability="storage.upload", operator="op"), {})  # external egress
    disp, reasons = mon.disposition(max_external_records=100)
    assert disp is GovernanceDisposition.DENY
    assert any("exfiltration" in r for r in reasons)


def test_plan_vs_observed_divergence_requires_review():
    ex, mon = _executor(planned=("crm.read", "report.generate"))
    ex.run(Node(capability="crm.read", operator="op"), {})
    ex.run(Node(capability="storage.upload", operator="op"), {})  # ran but was never planned
    disp, reasons = mon.disposition(max_external_records=100000)  # exfiltration off → only divergence fires
    assert disp is GovernanceDisposition.REQUIRE_REVIEW
    assert any("divergence" in r and "storage.upload" in r for r in reasons)


def test_failure_at_the_boundary_also_emits():
    ex, mon = _executor(upload_raises=True)
    with pytest.raises(OperatorError):
        ex.run(Node(capability="storage.upload", operator="op"), {})
    assert len(mon.trajectory.events) == 1                        # a refused side effect is still telemetry
    assert mon.trajectory.events[0].result == "error"


def test_enforce_drives_containment_end_to_end():
    from runtime_contracts import ContainmentState
    ex, mon = _executor()
    ex.run(Node(capability="crm.read", operator="op"), {})
    ex.run(Node(capability="storage.upload", operator="op"), {})
    disp, reasons, state = mon.enforce(max_external_records=100)
    assert disp is GovernanceDisposition.DENY and state is ContainmentState.CONTAINED
    assert mon.containment.history[-1] == ("CONTAINING", "CONTAINED")
