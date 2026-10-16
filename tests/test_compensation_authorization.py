"""Compensation authorization: Executor.compensate fails closed without evidence that THIS node executed
(and, for the approval marker, was approved) in THIS mission."""
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import Node


class Rec(InMemoryOperatorClient):
    def __init__(self):
        super().__init__({"do": lambda i: {"x": 1}, "undo": lambda i: {"undone": True}})
        self.undo_inputs = []

    def invoke(self, operator, capability, inputs, idempotency_key, *, secrets=None):
        if capability == "undo":
            self.undo_inputs.append(dict(inputs))
        return super().invoke(operator, capability, inputs, idempotency_key, secrets=secrets)


def _ex(c):
    ex = Executor(c)
    ex.undo_registry = lambda cap: {"do": "undo"}.get(cap)
    return ex


def _node():
    return Node(capability="do", operator="o", undo="undo", side_effecting=True, idempotency_key="k")


def _rt(store=None):
    c = Rec()
    ex = Executor(c)
    rt = MissionRuntime.__new__(MissionRuntime)
    rt.store = store or EventStore()
    ex.evidence_probe = rt._compensation_evidence
    ex.undo_registry = lambda cap: {"do": "undo"}.get(cap)
    return c, ex, rt


def test_never_executed_node_not_compensated():
    c = Rec(); ex = _ex(c)
    out = ex.compensate(_node(), mission_id="m1")
    assert out["compensation_skipped"] and out["undo_error"]
    assert c.undo_inputs == [] and c.calls == []


def test_ledger_approved_execution_mints_marker_once_and_is_idempotent():
    c = Rec(); ex = _ex(c); n = _node()
    ex.run(n, {"_mission": "m1", "_approval": {"approved": True}})
    r1 = ex.compensate(n, mission_id="m1"); r2 = ex.compensate(n, mission_id="m1")
    assert r1 == r2 == {"undone": True}
    assert len(c.undo_inputs) == 1
    assert c.undo_inputs[0]["_approval"] == {"approved": True, "compensation": True}


def test_other_mission_same_node_id_refused():
    c = Rec(); ex = _ex(c); n = _node()
    ex.run(n, {"_mission": "m1", "_approval": {"approved": True}})
    out = ex.compensate(n, mission_id="m2")
    assert out["compensation_skipped"] and c.undo_inputs == []


def test_policy_admitted_execution_compensates_with_marker():
    # ungated forward step (admitted by policy) with gated undo: rollback must still execute (charge -> refund)
    c = Rec(); ex = _ex(c); n = _node()
    ex.run(n, {"_mission": "m1"})
    ex.compensate(n, mission_id="m1")
    assert c.undo_inputs[0]["_approval"]["compensation"] is True


def test_client_supplied_approval_in_node_inputs_irrelevant_to_unexecuted():
    c = Rec(); ex = _ex(c)
    n = _node(); n.inputs = {"_approval": {"approved": True}}
    assert ex.compensate(n, mission_id="m1")["compensation_skipped"]
    assert c.undo_inputs == []


def _log(store, mid, *evs):
    for t, p in evs:
        store.append(t, mid, p)


def test_event_log_approved_survives_restart_and_wrong_mission():
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "do", "result": {"x": 1}}))
    n = _node(); n.id = "N"
    c, ex, _ = _rt(store)          # "restarted": fresh executor, same log
    assert ex.compensate(n, mission_id="m1") == {"undone": True}
    assert c.undo_inputs[0]["_approval"]["compensation"] is True
    assert ex.compensate(n, mission_id="m2")["compensation_skipped"]
    assert len(c.undo_inputs) == 1


def test_event_log_double_compensation_after_restart_is_noop():
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "do", "result": {"x": 1}}),
         ("NodeCompensated", {"node_id": "N", "result": {"undone": True}}))
    n = _node(); n.id = "N"
    c, ex, _ = _rt(store)
    assert ex.compensate(n, mission_id="m1") == {"undone": True}
    assert c.undo_inputs == []


def test_event_log_no_success_or_no_approval_or_invalidated():
    n = _node(); n.id = "N"
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeFailed", {"node_id": "N"}))
    c, ex, _ = _rt(store)
    assert ex.compensate(n, mission_id="m1")["compensation_skipped"] and c.undo_inputs == []
    # approval invalidated BEFORE execution: evidence of execution is what matters -> still compensable
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("ApprovalInvalidated", {"node_id": "N"}), ("NodeSucceeded", {"node_id": "N", "capability": "do", "result": {"x": 1}}))
    c, ex, _ = _rt(store); ex.compensate(n, mission_id="m1")
    assert len(c.undo_inputs) == 1
    # invalidated AFTER execution -> still compensable with the marker (documented, not widened)
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "do", "result": {"x": 1}}), ("ApprovalInvalidated", {"node_id": "N"}))
    c, ex, _ = _rt(store); ex.compensate(n, mission_id="m1")
    assert c.undo_inputs[0]["_approval"]["compensation"] is True


def test_capability_mismatch_refused():
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "other"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "other", "result": {"x": 1}}))
    n = _node(); n.id = "N"
    c, ex, _ = _rt(store)
    assert ex.compensate(n, mission_id="m1")["compensation_skipped"] and c.undo_inputs == []


def test_staged_forward_result_not_ledgered():
    c = Rec()
    c._handlers["do"] = lambda i: {"status": "pending_approval", "executed": False}
    ex = _ex(c); n = _node()
    ex.run(n, {"_mission": "m1", "_approval": {"approved": True}})
    out = ex.compensate(n, mission_id="m1")
    assert out["compensation_skipped"] and c.undo_inputs == []
    c._handlers["do"] = lambda i: {"executed": False}
    n2 = _node(); n2.id = "n2"; n2.idempotency_key = "k2"
    ex.run(n2, {"_mission": "m1"})
    assert ex.compensate(n2, mission_id="m1")["compensation_skipped"]


def test_blank_mission_refused_and_same_id_other_node_refused():
    c = Rec(); ex = _ex(c); n = _node()
    ex.run(n, {})                                   # no _mission => not ledgered
    assert ex.compensate(n)["compensation_skipped"]
    assert ex.compensate(n, mission_id="  ")["compensation_skipped"]
    ex.run(n, {"_mission": "m1"})
    other = _node(); other.idempotency_key = "different"
    assert ex.compensate(other, mission_id="m1")["compensation_skipped"]
    assert c.undo_inputs == []


def test_ledger_is_bounded():
    c = Rec(); ex = _ex(c); ex._LEDGER_CAP = 3
    for i in range(10):
        n = _node(); n.id = f"n{i}"; n.idempotency_key = f"k{i}"
        ex.run(n, {"_mission": "m1"})
    assert len(ex._ledger) == 3


def test_planner_attached_undo_not_registered_is_staged():
    c = Rec(); c._handlers["other"] = lambda i: {"done": True}
    ex = _ex(c); n = _node(); n.undo = "other"
    ex.run(n, {"_mission": "m1"})
    out = ex.compensate(n, mission_id="m1")
    assert out["compensation_skipped"] and "undo_not_registered" in out["undo_error"]
    assert [x for x in c.calls if x[0] == "other"] == []


def test_no_registry_wired_never_mints_marker():
    c = Rec(); ex = Executor(c); n = _node()
    ex.run(n, {"_mission": "m1"})
    ex.compensate(n, mission_id="m1")
    assert "_approval" not in c.undo_inputs[0]


def test_undo_inputs_come_from_logged_result_not_node_fields():
    store = EventStore()
    _log(store, "m1", ("NodeSucceeded", {"node_id": "N", "capability": "do", "result": {"charge_id": "real"}}))
    n = _node(); n.id = "N"; n.result = {"charge_id": "TAMPERED"}; n.inputs = {"amount": 999999}
    c, ex, _ = _rt(store)
    ex.compensate(n, mission_id="m1")
    assert c.undo_inputs[0]["result"] == {"charge_id": "real"}
    assert "amount" not in c.undo_inputs[0]


def test_probe_ignores_staged_nodesucceeded():
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "do",
                            "result": {"status": "pending_approval", "executed": False}}))
    n = _node(); n.id = "N"
    c, ex, _ = _rt(store)
    assert ex.compensate(n, mission_id="m1")["compensation_skipped"] and c.undo_inputs == []


def test_executor_shared_by_two_runtimes_rejected():
    from agentic_os.mission.registry import CapabilityRegistry
    ex = Executor(Rec())
    r1 = MissionRuntime(CapabilityRegistry(), ex, store=EventStore())
    r2 = MissionRuntime(CapabilityRegistry(), ex, store=EventStore())
    m = type("M", (), {"id": "m"})()
    r1._executor_for(m)
    import pytest
    with pytest.raises(RuntimeError):
        r2._executor_for(m)


import pytest

_NOT_EXECUTED = [{}, {"staged": True}, {"status": "staged"}, {"status": "held"},
                 {"result": {"status": "pending_approval"}}, {"result": {"staged": True}},
                 {"status": "pending_approval", "executed": False}, {"executed": False}]


@pytest.mark.parametrize("shape", _NOT_EXECUTED)
def test_not_executed_shapes_ledger_and_probe(shape):
    c = Rec(); c._handlers["do"] = lambda i, s=shape: dict(s)
    ex = _ex(c); n = _node()
    ex.run(n, {"_mission": "m1"})
    assert ex.compensate(n, mission_id="m1")["compensation_skipped"]
    store = EventStore()
    _log(store, "m1", ("NodeSucceeded", {"node_id": "N", "capability": "do", "result": shape}))
    n2 = _node(); n2.id = "N"
    c2, ex2, _ = _rt(store)
    assert ex2.compensate(n2, mission_id="m1")["compensation_skipped"]
    assert c.undo_inputs == [] and c2.undo_inputs == []


def test_real_result_still_proves_execution():
    from agentic_os.mission.verify import proves_execution
    assert proves_execution({"charge_id": "c1"}) and proves_execution({"ok": True, "result": {"id": 1}})
