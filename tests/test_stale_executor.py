"""A SUPERSEDED executor's late answer is evidence, never a step failure and never a reason to compensate.

Another executor can take a step over (its reservation is reconciled/settled by someone else, or the operator says outright that it
lost its claim). When the first executor's call then returns or raises, the base used to treat an exception as the step's failure and run
the saga, compensating work another executor had completed. Now: the late answer is recorded as stale evidence
(``StaleResultRecorded``), the step is UNCERTAIN, the mission is parked for an operator (``WAITING_RECONCILIATION``), and nothing is
retried, committed or compensated automatically."""
from __future__ import annotations

from agentic_os.mission.authority import Grant
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime, StaleExecution
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import CapabilityManifest, MissionState
from tests.test_compensation_authority import NotApplied, _Planner, ev, guard_with, specs

COMPENSATE_ALL = (Grant("*"), Grant("compensate:res.a"), Grant("compensate:res.b"))


class Board:
    """Counts what reached the operators and lets the step under test act as 'another executor took it over' before it answers."""

    def __init__(self):
        self.calls, self.undone, self.rt, self.m = [], [], None, None
        self.takeover = None
        self.late = lambda: {"executed": True, "b": 1}

    def lease_of_b(self):
        for (mid, nid), ent in self.rt._leases().items():
            node = next(n for n in self.rt._plans[mid].graph.nodes if n.id == nid)
            if node.capability == "res.b":
                return ent[0]
        raise AssertionError("no lease for res.b")

    def handlers(self):
        def undo(tag):
            def h(i):
                self.undone.append(tag)
                return {"undone": True, "executed": True}
            return h

        def b(i):
            self.calls.append("res.b")
            if self.takeover is not None:
                self.takeover(self.lease_of_b())
            return self.late()
        return {"res.a": lambda i: (self.calls.append("res.a"), {"executed": True, "a": 1})[1], "res.b": b,
                "res.undo_a": undo("a"), "res.undo_b": undo("b"),
                "res.verify": lambda i: {"verified": True, "observed": {"gone": True}},
                "crm.finish": lambda i: (self.calls.append("crm.finish"), {"executed": True})[1]}


def resolved_by_reconciliation(board, lease_id, *, executed):
    """What `reconcile_node` / the probe supervisor do to a reservation when another executor (or a person) resolves it: the ledger transition AND the
    `reconcile_status` record. (The executor settling its OWN reservation, as an operator does after executing, leaves no such record.)"""
    assert board.rt.authority.reconcile(lease_id, executed=executed)
    board.rt.authority.annotate(lease_id, reconcile_status="resolved_manually", resolved_by="another-executor")


def run(board, *, guard=None):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", specs()))
    extra = dict(authority=guard, authority_key=lambda m: "env-1") if guard is not None else {}
    rt = MissionRuntime(reg, Executor(InMemoryOperatorClient(board.handlers())), planner=_Planner("a", "b", "finished"),
                        store=EventStore(), **extra)
    m = rt.create_mission("go", policy_refs=["*"], failure_policy="COMPENSATE_DECLARED", tenant="acme" if guard is not None else "")
    board.rt, board.m = rt, m
    rt.run(m.id)
    return rt, m


def types_of(rt, m):
    return [e.type for e in rt.store.for_mission(m.id)]


def assert_parked_not_failed_not_compensated(rt, m, board):
    t = types_of(rt, m)
    assert m.state is MissionState.WAITING_RECONCILIATION, m.state
    assert "ReconciliationRequired" in t and "StaleResultRecorded" in t
    for forbidden in ("NodeFailed", "CompensationRequested", "CompensationExecuted", "CompensationAuthorized", "NodeCompensated", "MissionOutcome"):
        assert forbidden not in t, forbidden
    assert board.undone == []                                              # nothing was undone on anybody's behalf
    assert board.calls.count("res.b") == 1 and "crm.finish" not in board.calls   # nothing was retried, nothing downstream ran


# ── an exception from an executor whose reservation another executor already settled ─────────────────────────────────
def test_a_late_exception_after_the_reservation_was_settled_elsewhere_is_stale_evidence_and_never_runs_the_saga():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: resolved_by_reconciliation(board, lid, executed=True)      # another executor finished the step and settled it
    board.late = lambda: (_ for _ in ()).throw(RuntimeError("connection reset after the request was sent"))
    rt, m = run(board, guard=guard)
    assert_parked_not_failed_not_compensated(rt, m, board)
    rec = ev(rt, m.id, "StaleResultRecorded")[0]
    assert rec["kind"] == "failure" and rec["capability"] == "res.b" and rec["lease_state"] == "settled" and "connection reset" in rec["detail"]
    assert rec["action"].startswith("parked for operator") and "nothing retried" in rec["action"]


def test_a_late_exception_that_claims_nothing_was_applied_is_still_not_acted_on_when_the_step_was_taken_over():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: resolved_by_reconciliation(board, lid, executed=True)
    board.late = lambda: (_ for _ in ()).throw(NotApplied("the old request was refused"))     # not_applied is a claim about THAT request only
    rt, m = run(board, guard=guard)
    assert_parked_not_failed_not_compensated(rt, m, board)


def test_a_late_success_after_the_reservation_was_released_elsewhere_is_not_committed():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: resolved_by_reconciliation(board, lid, executed=False)     # someone proved it did not apply and returned the reservation
    rt, m = run(board, guard=guard)                                                    # ... and then this executor answers with a success
    assert_parked_not_failed_not_compensated(rt, m, board)
    assert "NodeSucceeded" not in [e.type for e in rt.store.for_mission(m.id) if e.payload.get("node_id") and e.payload.get("capability") == "res.b"]
    rec = ev(rt, m.id, "StaleResultRecorded")[0]
    assert rec["kind"] == "result" and rec["lease_state"] == "released"


# ── an executor that says outright that it lost its claim (a newer generation exists) ───────────────────────────────
def test_an_exception_marked_stale_generation_is_stale_evidence_even_without_an_authority_ledger():
    board = Board()
    board.late = lambda: (_ for _ in ()).throw(StaleExecution("generation 1 was superseded by generation 2"))
    rt, m = run(board)
    t = types_of(rt, m)
    assert m.state is MissionState.WAITING_RECONCILIATION and "StaleResultRecorded" in t and "ReconciliationRequired" in t
    assert not {"NodeFailed", "CompensationRequested", "CompensationExecuted", "MissionOutcome"} & set(t)
    assert board.undone == [] and board.calls.count("res.b") == 1 and "crm.finish" not in board.calls
    assert ev(rt, m.id, "StaleResultRecorded")[0]["kind"] == "failure"


def test_any_object_declaring_stale_generation_is_treated_the_same_way():
    class Straggler(RuntimeError):
        stale_generation = True
    board = Board()
    board.late = lambda: (_ for _ in ()).throw(Straggler("superseded"))
    rt, m = run(board)
    assert m.state is MissionState.WAITING_RECONCILIATION and board.undone == []


# ── the ordinary failure of the CURRENT holder is unchanged ─────────────────────────────────────────────────────────
def test_a_definite_failure_of_the_current_executor_still_compensates():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.late = lambda: (_ for _ in ()).throw(NotApplied("the provider refused this request"))
    rt, m = run(board, guard=guard)
    t = types_of(rt, m)
    assert "NodeFailed" in t and "StaleResultRecorded" not in t
    assert sorted(board.undone) == ["a"] and m.state is MissionState.FAILED


def test_an_unknown_failure_of_the_current_executor_still_parks_without_a_stale_record():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.late = lambda: (_ for _ in ()).throw(RuntimeError("timeout after the request was sent"))
    rt, m = run(board, guard=guard)
    t = types_of(rt, m)
    assert m.state is MissionState.WAITING_RECONCILIATION and "ReconciliationRequired" in t and "StaleResultRecorded" not in t
    assert board.undone == [] and "NodeFailed" not in t


def test_a_normal_success_is_unchanged():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    rt, m = run(board, guard=guard)
    t = types_of(rt, m)
    assert "StaleResultRecorded" not in t and board.calls == ["res.a", "res.b", "crm.finish"] and m.state is MissionState.SUCCEEDED


def test_a_reservation_already_marked_unknown_by_someone_else_is_the_ordinary_park_not_a_stale_record():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: board.rt.authority.unknown(lid)                       # a watchdog marked the outcome unknown while the call was in flight
    board.late = lambda: (_ for _ in ()).throw(RuntimeError("timeout after the request was sent"))
    rt, m = run(board, guard=guard)
    t = types_of(rt, m)
    assert m.state is MissionState.WAITING_RECONCILIATION and "ReconciliationRequired" in t
    assert "StaleResultRecorded" not in t and "NodeFailed" not in t and board.undone == []


def test_an_executor_that_settles_its_own_reservation_and_then_answers_is_the_normal_path_not_a_takeover():
    """The operator settles the authorization it executed under before the runtime hears the answer (operator_sdk). That is not a stale answer."""
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: board.rt.authority.settle(lid)
    rt, m = run(board, guard=guard)
    t = types_of(rt, m)
    assert "StaleResultRecorded" not in t and m.state is MissionState.SUCCEEDED and board.calls == ["res.a", "res.b", "crm.finish"]


def test_an_executor_that_settles_its_own_reservation_and_then_fails_is_still_a_failure_of_the_step():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: board.rt.authority.settle(lid)
    board.late = lambda: (_ for _ in ()).throw(NotApplied("the provider refused the request"))
    rt, m = run(board, guard=guard)
    t = types_of(rt, m)
    assert "StaleResultRecorded" not in t and "NodeFailed" in t and sorted(board.undone) == ["a"]


def test_a_reservation_that_is_gone_from_the_ledger_is_treated_as_superseded_and_parks():
    guard, _ = guard_with(*COMPENSATE_ALL)
    board = Board()
    board.takeover = lambda lid: setattr(board.rt.authority, "lease", lambda _lid: None)       # the record behind the reservation cannot be found
    board.late = lambda: (_ for _ in ()).throw(RuntimeError("connection reset after the request was sent"))
    rt, m = run(board, guard=guard)
    assert_parked_not_failed_not_compensated(rt, m, board)
    assert ev(rt, m.id, "StaleResultRecorded")[0]["lease_state"] == "gone"
