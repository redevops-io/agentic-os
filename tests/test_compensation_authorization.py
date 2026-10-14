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


def _node():
    return Node(capability="do", operator="o", undo="undo", side_effecting=True, idempotency_key="k")


def _rt(store=None):
    c = Rec()
    ex = Executor(c)
    rt = MissionRuntime.__new__(MissionRuntime)
    rt.store = store or EventStore()
    ex.evidence_probe = rt._compensation_evidence
    return c, ex, rt


def test_never_executed_node_not_compensated():
    c = Rec(); ex = Executor(c)
    out = ex.compensate(_node(), mission_id="m1")
    assert out["compensation_skipped"] and out["undo_error"]
    assert c.undo_inputs == [] and c.calls == []


def test_ledger_approved_execution_mints_marker_once_and_is_idempotent():
    c = Rec(); ex = Executor(c); n = _node()
    ex.run(n, {"_mission": "m1", "_approval": {"approved": True}})
    r1 = ex.compensate(n, mission_id="m1"); r2 = ex.compensate(n, mission_id="m1")
    assert r1 == r2 == {"undone": True}
    assert len(c.undo_inputs) == 1
    assert c.undo_inputs[0]["_approval"] == {"approved": True, "compensation": True}


def test_other_mission_same_node_id_refused():
    c = Rec(); ex = Executor(c); n = _node()
    ex.run(n, {"_mission": "m1", "_approval": {"approved": True}})
    out = ex.compensate(n, mission_id="m2")
    assert out["compensation_skipped"] and c.undo_inputs == []


def test_policy_admitted_execution_compensates_with_marker():
    # ungated forward step (admitted by policy) with gated undo: rollback must still execute (charge -> refund)
    c = Rec(); ex = Executor(c); n = _node()
    ex.run(n, {"_mission": "m1"})
    ex.compensate(n, mission_id="m1")
    assert c.undo_inputs[0]["_approval"]["compensation"] is True


def test_client_supplied_approval_in_node_inputs_irrelevant_to_unexecuted():
    c = Rec(); ex = Executor(c)
    n = _node(); n.inputs = {"_approval": {"approved": True}}
    assert ex.compensate(n, mission_id="m1")["compensation_skipped"]
    assert c.undo_inputs == []


def _log(store, mid, *evs):
    for t, p in evs:
        store.append(t, mid, p)


def test_event_log_approved_survives_restart_and_wrong_mission():
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "do"}))
    n = _node(); n.id = "N"
    c, ex, _ = _rt(store)          # "restarted": fresh executor, same log
    assert ex.compensate(n, mission_id="m1") == {"undone": True}
    assert c.undo_inputs[0]["_approval"]["compensation"] is True
    assert ex.compensate(n, mission_id="m2")["compensation_skipped"]
    assert len(c.undo_inputs) == 1


def test_event_log_double_compensation_after_restart_is_noop():
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "do"}),
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
         ("ApprovalInvalidated", {"node_id": "N"}), ("NodeSucceeded", {"node_id": "N", "capability": "do"}))
    c, ex, _ = _rt(store); ex.compensate(n, mission_id="m1")
    assert len(c.undo_inputs) == 1
    # invalidated AFTER execution -> still compensable with the marker (documented, not widened)
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "do"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "do"}), ("ApprovalInvalidated", {"node_id": "N"}))
    c, ex, _ = _rt(store); ex.compensate(n, mission_id="m1")
    assert c.undo_inputs[0]["_approval"]["compensation"] is True


def test_capability_mismatch_refused():
    store = EventStore()
    _log(store, "m1", ("ApprovalGranted", {"node_id": "N", "capability": "other"}),
         ("NodeSucceeded", {"node_id": "N", "capability": "other"}))
    n = _node(); n.id = "N"
    c, ex, _ = _rt(store)
    assert ex.compensate(n, mission_id="m1")["compensation_skipped"] and c.undo_inputs == []
