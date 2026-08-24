"""Mission-native tracing: the Mission is the root; its event trajectory projects into one nested span
tree; a substrate context nests underneath the causing node. Skips if runtime-contracts is absent."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("runtime_contracts")

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.security_monitor import SecurityMonitor
from agentic_os.mission.tracing import MissionTrace
from agentic_os.mission.types import Node

DESCS = {
    "crm.read": SimpleNamespace(data_classifications=("pii",), required_authority=("read:crm",), network=()),
    "storage.upload": SimpleNamespace(data_classifications=(), required_authority=("write:storage",),
                                      network=("s3.external.com",)),
}


def test_trajectory_projects_into_one_rooted_span_tree():
    mon = SecurityMonitor(mission_id="m-7F21", descriptor_for=DESCS.get)
    ex = Executor(InMemoryOperatorClient({"crm.read": lambda i: {"records": 3},
                                          "storage.upload": lambda i: {}}), monitor=mon)
    ex.run(Node(capability="crm.read", operator="op"), {})
    ex.run(Node(capability="storage.upload", operator="op"), {})

    trace = MissionTrace("m-7F21")
    spans = trace.spans(mon.trajectory)
    assert len(spans) == 2
    # every span shares the one Mission trace_id
    assert {s["trace_id"] for s in spans} == {trace.root.trace_id}
    # the network egress span carries the endpoint as a reference attribute
    upload = next(s for s in spans if s["name"] == "storage.upload")
    assert upload["attributes"]["redevops.network"] == ["s3.external.com"]
    assert upload["attributes"]["redevops.mission_id"] == "m-7F21"


def test_substrate_context_nests_under_the_mission_node():
    trace = MissionTrace("m1", intent_id="deploy")
    node = trace.node("deploy-svc")
    sub = trace.substrate_context("deploy-svc", capability="argo.submit")
    # Argo's spans continue the SAME trace, parented under the mission node's span
    assert sub.trace_id == trace.root.trace_id
    assert sub.parent_span_id == node.span_id
    # what we hand Argo: a W3C traceparent + mission baggage
    tp = sub.traceparent()
    assert tp.startswith("00-") and sub.baggage()["redevops.mission_id"] == "m1"
