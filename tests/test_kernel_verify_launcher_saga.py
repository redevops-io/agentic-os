"""Round-4 kernel fixes: (A) N4 read-back classifier + value-aware verifier, (B) launcher plan inspection,
(C) honest saga reporting."""
from __future__ import annotations

import types

import pytest

from agentic_os.app_kit.decision_bridge import BridgeError, LaunchRefused, MissionRequest
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher, auto_approve_for_tests, drive
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability, pending_approval
from agentic_os.mission.planner import ExecutionIntent, IntentStep
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (CapabilityManifest, CapabilitySpec, MissionState, Node,
                                      VerificationDecision)
from agentic_os.mission.verify import CompositeVerifier, result_failed

FAILED_SHAPES = [
    {"success": False}, {"errors": ["boom"]}, {"status_code": 403}, {"http_status": 502}, {"lago_status": 500},
    {"status": "ERR"}, {"status": "forbidden"}, {"status": "unavailable"}, {"status": "timeout"},
    {"status": "conflict"}, {"status": "partial"}, {"status": "undeliverable"}, {"status": "not_configured"},
    {"detail": "nope", "status_code": 404}, {"status": "pending_approval", "executed": False, "k": False},
    {"status": "error", "k": True}, {"refund_staged": True, "status": "error"}, {"ok": False},
]


@pytest.mark.parametrize("shape", FAILED_SHAPES)
def test_classifier_flags_failure_shapes(shape):
    failed, reason = result_failed(shape)
    assert failed and reason


@pytest.mark.parametrize("shape", [{"k": True}, {"status": "ok", "count": 0}, {"status": "sent", "errors": []},
                                   {"status_code": 200}])
def test_classifier_accepts_success_shapes(shape):
    assert result_failed(shape)[0] is False


@pytest.mark.parametrize("shape", FAILED_SHAPES)
def test_operator_never_mints_markers_from_failure_shapes(shape):
    shape = {kk: vv for kk, vv in shape.items() if kk != "k"}
    op = Operator("o", [capability("c", lambda i, s=shape: dict(s), outputs={"k": "boolean"})])
    assert "k" not in op.invoke("c", {}, "")


def _verify(result, outputs=None, assertions=None):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("o", [CapabilitySpec(
        "c", "o", provides=["k"], outputs=outputs if outputs is not None else {"k": "boolean"},
        side_effecting=True, permissions=["*"])]))
    node = Node(capability="c", operator="o", side_effecting=True)
    v = CompositeVerifier(reg, lambda m: {"*"})
    return v.verify(node, result, None, types.SimpleNamespace(policy_refs=["*"])).decision


@pytest.mark.parametrize("shape", FAILED_SHAPES)
def test_verifier_rejects_failed_results_even_with_declared_key_true(shape):
    assert _verify({"k": True, **shape}) is VerificationDecision.REJECT


@pytest.mark.parametrize("val", [False, None, ""])
def test_verifier_checks_declared_output_values_not_presence(val):
    assert _verify({"k": val}) is VerificationDecision.REJECT


def test_verifier_bool_ish_zero_fails_but_int_zero_is_fine():
    assert _verify({"k": 0}) is VerificationDecision.REJECT
    assert _verify({"n": 0}, outputs={"n": "integer"}) is VerificationDecision.ACCEPT
    assert _verify({}) is VerificationDecision.REJECT           # empty still fails
    assert _verify({"k": True}) is VerificationDecision.ACCEPT


def test_pending_approval_still_returned_unchanged_on_ungoverned_http_path():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    op = Operator("o", [capability("c", lambda i: pending_approval("c", k=False),
                                   outputs={"k": "boolean"}, side_effecting=True, approval_required=True)])
    app = FastAPI()
    app.include_router(op.router())
    body = TestClient(app).post("/invoke", json={"capability": "c", "inputs": {}}).json()["result"]
    assert body["status"] == "pending_approval" and body["executed"] is False and body["k"] is False


class _OnePlanner:
    def __init__(self, *steps):
        self.steps = steps

    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=list(self.steps))


def test_ungated_node_returning_staged_result_fails_not_commits():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("o", [CapabilitySpec(
        "stage", "o", provides=["done"], outputs={"done": "boolean"}, permissions=["*"])]))
    op = Operator("o", [capability("stage", lambda i: pending_approval("stage", done=False),
                                   provides=["done"], outputs={"done": "boolean"})])
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({"o": op})),
                        planner=_OnePlanner(IntentStep(outcome="done", need="do it")))
    m = rt.create_mission("do it", policy_refs=["*"])
    rt.run(m.id)
    assert m.state == MissionState.FAILED
    failed = [e for e in rt.repo.timeline(m.id) if e["type"] == "NodeFailed"]
    assert failed and failed[0]["payload"]["reason"] == "staged_not_executed"
    assert "done" not in str(rt._world(m.id).snapshot())


# ── B. launcher ──────────────────────────────────────────────────────────────
def _req(cap="demo.send_email", goal="clean up", **kw):
    return MissionRequest(goal=goal, capability=cap, required_capabilities=(cap,),
                          inputs={"a": 1}, gated=True, intervention_id="iv-9", opportunity_id="op",
                          action_kind="send", subject="S", principal=kw.get("principal", "alice"))


class _Stub:
    def __init__(self, nodes):
        self.nodes, self._plans, self.created, self.cancelled, self.ran = nodes, {}, {}, [], False

    def create_mission(self, goal, *, policy_refs=None, inputs=None, principal=None, intervention_id=""):
        self.created = {"principal": principal, "intervention_id": intervention_id}
        self._plans["m-1"] = types.SimpleNamespace(graph=types.SimpleNamespace(nodes=[
            types.SimpleNamespace(capability=c, side_effecting=se) for c, se in self.nodes]))
        return types.SimpleNamespace(id="m-1")

    def cancel_mission(self, mid, reason=""):
        self.cancelled.append(mid)

    def run(self, mid):
        self.ran = True


def test_launcher_refuses_extra_side_effecting_capability_and_cancels():
    rt = _Stub([("demo.send_email", True), ("demo.delete_all", True)])
    with pytest.raises(LaunchRefused):
        MissionRuntimeLauncher(rt).launch(_req())
    assert rt.cancelled == ["m-1"] and not rt.ran
    assert issubclass(LaunchRefused, BridgeError)


def test_launcher_allows_read_only_extras_and_carries_principal_and_intervention():
    rt = _Stub([("demo.lookup", False), ("demo.send_email", True)])
    assert MissionRuntimeLauncher(rt).launch(_req()) == "m-1"
    assert rt.created == {"principal": "alice", "intervention_id": "iv-9"}


def test_launcher_refuses_when_plan_cannot_be_inspected():
    rt = _Stub([])
    with pytest.raises(LaunchRefused):
        MissionRuntimeLauncher(rt).launch(_req())


def test_real_runtime_records_intervention_and_principal_on_mission():
    op = Operator("demo", [capability("demo.send_email", lambda i: {"sent": True}, provides=["sent"],
                                      outputs={"sent": "boolean"})])
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({"demo": op})))
    mid = MissionRuntimeLauncher(rt).launch(_req(goal="sent"))
    mission = rt._missions[mid]
    assert mission.intervention_id == "iv-9" and mission.principal == "alice"
    created = [e for e in rt.repo.timeline(mid) if e["type"] == "MissionCreated"][0]
    assert created["payload"]["intervention_id"] == "iv-9"


def test_drive_requires_explicit_approver():
    with pytest.raises(TypeError):
        drive(_Stub([]), "m-1")          # type: ignore[call-arg]
    assert auto_approve_for_tests("m", "n") == "approve"


def test_drive_without_approving_leaves_gate_parked():
    op = Operator("demo", [capability("demo.send_email", lambda i: {"sent": True, "executed": True},
                                      provides=["sent"], outputs={"sent": "boolean"},
                                      side_effecting=True, approval_required=True)])
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({"demo": op})))
    mid = MissionRuntimeLauncher(rt).launch(_req(goal="sent"))
    mission = drive(rt, mid, approver=lambda m, n: "reject")
    assert mission.state == MissionState.FAILED


# ── C. saga ──────────────────────────────────────────────────────────────────
class _SagaPlanner:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[
            IntentStep(outcome="charged", need="charge the customer"),
            IntentStep(outcome="notified", need="notify the customer", inputs_from=["charged"])])


def _saga_rt(undo):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("billing", [CapabilitySpec(
        "charge", "billing", provides=["charged"], side_effecting=True, undo="refund", permissions=["*"])]))
    reg.register(CapabilityManifest("email", [CapabilitySpec(
        "notify", "email", provides=["notified"], side_effecting=True, permissions=["*"])]))
    handlers = {"charge": lambda i: {"charge_id": "c1"},
                "notify": lambda i: (_ for _ in ()).throw(RuntimeError("smtp down")), "refund": undo}
    rt = MissionRuntime(reg, Executor(InMemoryOperatorClient(handlers)), planner=_SagaPlanner())
    m = rt.create_mission("charge and notify", policy_refs=["*"])
    rt.run(m.id)
    return rt, m


def _types(rt, m):
    return [e["type"] for e in rt.repo.timeline(m.id)]


@pytest.mark.parametrize("undo", [
    lambda i: pending_approval("refund"),
    lambda i: {"status": "error", "error": "gateway"},
    lambda i: (_ for _ in ()).throw(RuntimeError("down")),
], ids=["staged", "errored", "raised"])
def test_failed_undo_records_compensation_failed_and_incomplete_outcome(undo):
    rt, m = _saga_rt(undo)
    assert m.state == MissionState.FAILED
    t = _types(rt, m)
    assert "NodeCompensationFailed" in t and "NodeCompensated" not in t
    assert m.outcome["compensation_incomplete"] is True
    assert len(m.outcome["compensation_remaining"]) == 1
    ex = rt.explain(m.id)
    assert ex["compensation_incomplete"] is True and ex["compensation_remaining"]
    assert any(s["type"] == "NodeCompensationFailed" for s in ex["steps"])


def test_successful_undo_still_records_compensated():
    rt, m = _saga_rt(lambda i: {"refunded": True})
    assert "NodeCompensated" in _types(rt, m) and "NodeCompensationFailed" not in _types(rt, m)
    assert not (m.outcome or {}).get("compensation_incomplete")
