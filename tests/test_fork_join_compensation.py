"""Fork/join x compensation contracts (PR #450): the MISSION's failure policy decides.
Requires feat/compensation-contracts (compensation_policy / compensation_runner)."""
from __future__ import annotations

from agentic_os.mission import compensation as C
from agentic_os.mission.fork_join import BranchSpec, ForkJoinController, ForkSpec, JoinSpec, branch_node_id
from agentic_os.mission.types import MissionState
from tests._fork_support import build, types


def _fork(cap_ok="w.write", extra=()):
    """Serial fork: b0 ok (reversible), b1 ok (cap_ok2), b2 fails -> ALL join fails after b0/b1 succeeded."""
    brs = [BranchSpec("b0", "w.write", inputs={"tag": "b0"}),
           BranchSpec("b1", cap_ok, inputs={"tag": "b1"}),
           BranchSpec("b2", "w.flaky", inputs={"tag": "b2"})]
    return ForkSpec("f1", brs, JoinSpec("all"), max_concurrent=1)


def _setup(policy="", cap_ok="w.write", failing=("w.flaky",)):
    rt, op, m = build(failure_policy=policy)
    op.fail_caps = set(failing)
    rt.forks.run_fork(m.id, _fork(cap_ok))
    return rt, op, m


def _undo_calls(op):
    return sorted(k for k in op.calls if k.endswith(":undo"))


def test_graph_forks_is_the_single_source_for_forked_topology():
    rt, op, m = _setup()
    assert rt._plans[m.id].graph.forks == ["f1"]


def test_default_halt_keeps_sibling_work_and_compensates_nothing():
    rt, op, m = _setup()
    st = rt.forks.state(m.id, "f1")
    assert st.ids("succeeded") == ["b0", "b1"] and st.ids("failed") == ["b2"]
    assert all(b.result for b in st.ordered() if b.status == "succeeded")
    t = types(rt, m.id)
    assert not any(x.startswith("Compensation") or x == "NodeCompensated" for x in t)
    assert _undo_calls(op) == [] and m.state == MissionState.FAILED
    assert m.outcome["failure_policy"] == "HALT_AND_REPORT" and m.outcome["rolled_back"] is False
    assert m.outcome["not_compensated"] == [branch_node_id(m.id, "f1", "b0"), branch_node_id(m.id, "f1", "b1")][::-1]
    j = next(e for e in rt.store.for_mission(m.id) if e.type == "JoinFailed").payload
    assert j["decision"]["action"] == "halt_and_report" and j["decision"]["policy"] == "HALT_AND_REPORT"


def test_halt_drains_inflight_siblings_instead_of_cancelling_completed_work():
    rt, op, m = build(concurrency=3)
    op.fail_caps = {"w.flaky"}
    op.by_tag_delay = {"b0": 0.08, "b1": 0.08}
    brs = [BranchSpec("b0", "w.write", inputs={"tag": "b0"}), BranchSpec("b1", "w.write", inputs={"tag": "b1"}),
           BranchSpec("b2", "w.flaky", inputs={"tag": "b2"})]
    rt.forks.run_fork(m.id, ForkSpec("f1", brs, JoinSpec("all")))
    st = rt.forks.state(m.id, "f1")
    assert st.ids("succeeded") == ["b0", "b1"] and st.ids("cancelled") == []      # in-flight work kept
    assert _undo_calls(op) == []


def test_compensate_declared_undoes_only_declared_undos_of_succeeded_branches_once_each():
    rt, op, m = _setup("COMPENSATE_DECLARED", cap_ok="w.irrev")
    n0, n1, n2 = (branch_node_id(m.id, "f1", b) for b in ("b0", "b1", "b2"))
    folded = C.fold_compensation(rt.store.for_mission(m.id))
    assert set(folded) == {n0} and folded[n0]["status"] == "compensated"        # b1 irreversible, b2 failed
    assert len(_undo_calls(op)) == 1
    t = [e.type for e in rt.store.for_mission(m.id) if e.payload.get("node_id") == n0 and e.type.startswith("Compensation")]
    assert t == ["CompensationRequested", "CompensationAuthorized", "CompensationExecuted", "CompensationVerified"]
    nr = {x["node_id"]: x["reversibility"] for x in m.outcome["not_reversible"]}
    assert nr[n1] == "IRREVERSIBLE" and n2 not in nr   # reported, never "compensated"
    assert m.outcome["compensated"] == [n0] and m.outcome["rolled_back"] is False
    assert m.state == MissionState.FAILED
    # idempotent: re-driving the closed fork does not compensate again
    rt.forks.run_fork(m.id, _fork("w.irrev"))
    assert len(_undo_calls(op)) == 1 and types(rt, m.id).count("CompensationRequested") == 1


def test_compensation_runs_in_reverse_order_for_all_declared_branches():
    rt, op, m = _setup("COMPENSATE_DECLARED")
    n0, n1 = (branch_node_id(m.id, "f1", b) for b in ("b0", "b1"))
    reqs = [e.payload["node_id"] for e in rt.store.for_mission(m.id) if e.type == "CompensationRequested"]
    assert reqs == [n1, n0] and m.outcome["rolled_back"] is True   # every consequential effect verified undone


def test_compensation_failure_surfaces_as_compensation_failed():
    rt, op, m = _setup("COMPENSATE_DECLARED", failing=("w.flaky", "w.undo"))
    assert m.state == MissionState.COMPENSATION_FAILED
    assert m.outcome["compensation_incomplete"] is True and len(m.outcome["compensation_remaining"]) == 2
    assert types(rt, m.id).count("CompensationFailed") == 2 and m.outcome["rolled_back"] is False


def test_unrecognised_mission_policy_fails_safe_to_halt():
    rt, op, m = _setup("DO_EVERYTHING")
    assert _undo_calls(op) == [] and m.state == MissionState.FAILED


def test_hook_cannot_widen_the_mission_policy_into_compensation():
    from agentic_os.mission.fork_join import FailureDecision
    rt, op, m = build()
    rt.forks.on_branch_failure(lambda ctx, dec: FailureDecision(
        "compensate", "widen", compensate_nodes=(branch_node_id(m.id, "f1", "b0"),)))
    op.fail_caps = {"w.flaky"}
    rt.forks.run_fork(m.id, _fork())
    assert _undo_calls(op) == []
