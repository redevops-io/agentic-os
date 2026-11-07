"""A superseded executor's late answer must not re-open a mission whose LOG says it is already over.

The driver that receives the late answer has an in-memory view of the mission that can be behind the log (another driver finished the mission:
failed and compensated, cancelled, ...). Parking it for reconciliation would write ``waiting_reconciliation`` over a terminal state, and a later
``reconcile_node`` could then run the rest of a failed mission. The decision is made from the LOG's state:

  FAILED / CANCELLED / COMPENSATION_FAILED (the AOS-DD compensated mission is CANCELLED)  -> ``StaleResultRecorded`` as evidence only, the mission
      stays terminal, the possible duplicate effect is a residual of the step (the step is irreversible-uncertain), nothing is retried or compensated;
  SUCCEEDED -> unchanged: parked for an operator (the owner asked for it);   still running -> unchanged."""
from __future__ import annotations

import pytest

from agentic_os.mission.compensation_runner import outcome_from_log
from agentic_os.mission.reconcile import ReconciliationError
from agentic_os.mission.receipt import mission_receipt
from agentic_os.mission.types import MissionState
from tests._principals import authn
from tests.test_stale_executor import COMPENSATE_ALL, Board, ev, guard_with, resolved_by_reconciliation, run, types_of

TERMINAL = ["failed", "cancelled", "compensation_failed"]


def other_driver_ends_the_mission(board, state):
    """Another driver took the step over, finished the mission (the LOG now says `state`) and this executor's view has not caught up."""
    def hook(lease_id):
        resolved_by_reconciliation(board, lease_id, executed=True)
        board.rt.store.append("MissionStateChanged", board.m.id, {"state": state})
    return hook


def late_failure(board):
    board.late = lambda: (_ for _ in ()).throw(RuntimeError("connection reset after the request was sent"))


def last_state_in_log(rt, m):
    return [e.payload["state"] for e in rt.store.for_mission(m.id) if e.type == "MissionStateChanged"][-1]


def assert_evidence_only(rt, m, board, state):
    t = types_of(rt, m)
    assert last_state_in_log(rt, m) == state and rt.repo.state(m.id).value == state             # nothing was written over the terminal state
    assert m.state.value == state                                                              # and this driver's view follows the log
    assert "ReconciliationRequired" not in t and "NodeFailed" not in t
    assert not {"CompensationRequested", "CompensationExecuted", "CompensationAuthorized", "NodeCompensated"} & set(t)
    assert board.undone == [] and board.calls.count("res.b") == 1 and "crm.finish" not in board.calls
    rec = ev(rt, m.id, "StaleResultRecorded")
    assert len(rec) == 1 and rec[0]["terminal"] is True and rec[0]["mission_state"] == state and "evidence only" in rec[0]["action"] and rec[0]["capability"] == "res.b"
    return rec[0]


@pytest.mark.parametrize("state", TERMINAL)
def test_a_late_exception_after_the_log_says_the_mission_is_over_is_evidence_only(state):
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = other_driver_ends_the_mission(board, state)
    late_failure(board)
    rt, m = run(board, guard=guard)
    rec = assert_evidence_only(rt, m, board, state)
    assert rec["kind"] == "failure"


def test_a_late_success_after_the_log_says_the_mission_is_over_is_evidence_only_and_not_committed():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = other_driver_ends_the_mission(board, "failed")
    rt, m = run(board, guard=guard)                                                     # the default late answer is a success
    rec = assert_evidence_only(rt, m, board, "failed")
    assert rec["kind"] == "result"
    assert not [e for e in rt.store.for_mission(m.id) if e.type == "NodeSucceeded" and e.payload.get("capability") == "res.b"]


def test_the_possible_duplicate_is_a_residual_and_the_step_is_uncertain():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = other_driver_ends_the_mission(board, "failed")
    rt, m = run(board, guard=guard)
    out = outcome_from_log(rt, m.id)
    steps = {s["capability"]: s for s in out["step_classification"]}
    assert steps["res.b"]["classification"] == "irreversible-uncertain"
    dup = [r for r in out["residual_effects"] if r["capability"] == "res.b" and r["kind"] == "possible_duplicate"]
    assert dup and dup[0]["source"] == "stale_result" and "superseded" in dup[0]["effect"]
    assert out["rolled_back"] is False
    rc = mission_receipt(rt, m.id)
    assert [x["capability"] for x in rc["stale_results"]] == ["res.b"] and rc["stale_results"][0]["terminal"] is True


@pytest.mark.parametrize("executed", [True, False])
def test_a_persons_reconcile_node_cannot_resume_a_mission_that_a_stale_answer_arrived_at(executed):
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = other_driver_ends_the_mission(board, "failed")
    late_failure(board)
    rt, m = run(board, guard=guard)
    node = next(n for n in rt._plans[m.id].graph.nodes if n.capability == "res.b")
    n_before, ev_before = len(board.calls), len(rt.store.for_mission(m.id))
    with pytest.raises(ReconciliationError, match="no unresolved reconciliation"):
        rt.reconcile_node(m.id, node.id, executed=executed, evidence={"kind": "provider_record", "reference": "someone says so"}, reason="a person tries to continue it",
                          principal=authn("ops-person", tenant="*"), result={"x": 1} if executed else None)
    rt.run(m.id)                                                                         # a plain run does not revive it either
    assert len(board.calls) == n_before and last_state_in_log(rt, m) == "failed" and len(rt.store.for_mission(m.id)) == ev_before
    assert m.state is MissionState.FAILED


# ---- unchanged ----------------------------------------------------------------------------------------------------------------------------------
def test_a_succeeded_mission_is_still_parked_for_an_operator_by_a_late_stale_answer():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = other_driver_ends_the_mission(board, "succeeded")
    late_failure(board)
    rt, m = run(board, guard=guard)
    t = types_of(rt, m)
    assert m.state is MissionState.WAITING_RECONCILIATION and last_state_in_log(rt, m) == "waiting_reconciliation"
    assert "ReconciliationRequired" in t and "NodeFailed" not in t and board.undone == []
    rec = ev(rt, m.id, "StaleResultRecorded")[0]
    assert rec.get("terminal") is not True and rec["action"].startswith("parked for operator")


def test_a_running_mission_is_still_parked_by_a_stale_answer():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: resolved_by_reconciliation(board, lid, executed=True)
    late_failure(board)
    rt, m = run(board, guard=guard)
    assert m.state is MissionState.WAITING_RECONCILIATION and "ReconciliationRequired" in types_of(rt, m)
    assert ev(rt, m.id, "StaleResultRecorded")[0].get("terminal") is not True
