"""P10 — governance plane: versioned mid-flight policy edits, suspend/resume, audited promotion.

The console read-model is thin; the load-bearing work is policy versioning with unambiguous
semantics — terminal nodes keep the version they ran under, tightening invalidates open approvals,
and a retroactive revoke of authority a completed side effect relied on compensates and fails.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
)


def _fleet():
    """a: a reversible side effect (runs); b: mandatory-approval side effect (gates)."""
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [
        CapabilitySpec("op.a", "op", provides=["a"], side_effecting=True, undo="op.a.undo",
                       permissions=["a:write"]),
        CapabilitySpec("op.b", "op", provides=["b"], side_effecting=True, undo="op.b.undo",
                       approval_required=True, permissions=["b:write"]),
    ]))
    client = InMemoryOperatorClient({
        "op.a": lambda i: {"ok": "a"}, "op.a.undo": lambda i: {"undone": "a"},
        "op.b": lambda i: {"ok": "b"}, "op.b.undo": lambda i: {"undone": "b"},
    })
    return reg, client


class _TwoStep:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[
            IntentStep(outcome="a", need="do a"),
            IntentStep(outcome="b", need="do b", inputs_from=["a"])])


def _run_to_gate(policy_refs=("a:write", "b:write")):
    reg, client = _fleet()
    rt = MissionRuntime(reg, Executor(client), planner=_TwoStep())
    m = rt.create_mission("job", policy_refs=list(policy_refs))
    rt.run(m.id)                                  # a runs, b parks on its mandatory approval
    assert rt.repo.state(m.id) == MissionState.WAITING_HUMAN
    return rt, m


# ─── policy version increments and records the diff ──────────────────────────
def test_policy_version_increments_with_audit():
    rt, m = _run_to_gate()
    assert rt.governance.policy_version(m.id) == 1
    rt.change_policy(m.id, actor="admin", reason="add scope", grant=["c:write"])
    assert rt.governance.policy_version(m.id) == 2
    ch = rt.governance.policy_history(m.id)[-1]
    assert ch["actor"] == "admin" and "c:write" in ch["after_grants"]
    assert "c:write" not in ch["before_grants"]


# ─── non-retroactive: terminal nodes keep their version, aren't re-run ────────
def test_non_retroactive_change_preserves_completed_nodes():
    rt, m = _run_to_gate()
    a_runs_before = sum(1 for e in rt.repo.timeline(m.id)
                        if e["type"] == "NodeSucceeded" and e["payload"]["capability"] == "op.a")
    ch_ret = rt.change_policy(m.id, actor="admin", reason="widen", grant=["c:write"])
    ch = rt.governance.policy_history(m.id)[-1]
    a_node = [n.id for n in rt._plans[m.id].graph.nodes if n.capability == "op.a"][0]
    assert a_node in ch["ran_under_previous"] and a_node not in ch["affected_nodes"]
    a_runs_after = sum(1 for e in rt.repo.timeline(m.id)
                       if e["type"] == "NodeSucceeded" and e["payload"]["capability"] == "op.a")
    assert a_runs_after == a_runs_before          # completed side effect not re-executed


# ─── a tightening (revoke) invalidates an open approval ──────────────────────
def test_revoke_invalidates_open_approval():
    rt, m = _run_to_gate()
    b_node = rt.repo.pending_human(m.id)["node_id"]
    rt.change_policy(m.id, actor="secops", reason="revoke b", revoke=["b:write"])
    inval = [e for e in rt.repo.timeline(m.id) if e["type"] == "ApprovalInvalidated"]
    assert inval and inval[0]["payload"]["node_id"] == b_node


# ─── retroactive revoke of authority a completed side effect used → compensate + fail ─
def test_retroactive_revoke_compensates_and_fails():
    rt, m = _run_to_gate()
    rt.change_policy(m.id, actor="secops", reason="a:write was mis-granted",
                     revoke=["a:write"], retroactive=True)
    assert rt.repo.state(m.id) == MissionState.FAILED
    tl = rt.repo.timeline(m.id)
    assert any(e["type"] == "PolicyViolationDetected" for e in tl)
    assert any(e["type"] == "NodeCompensated" and e["payload"]["undo"] == "op.a.undo" for e in tl)


# ─── suspend halts the mission; resume continues it ──────────────────────────
def test_suspend_and_resume():
    rt, m = _run_to_gate()
    rt.suspend(m.id, actor="oncall", reason="incident")
    assert rt.repo.state(m.id) == MissionState.PAUSED
    resumed = rt.resume(m.id, actor="oncall")
    assert resumed.state in (MissionState.WAITING_HUMAN, MissionState.RUNNING)   # back on the gate
    assert any(e["type"] == "MissionResumed" for e in rt.repo.timeline(m.id))


# ─── promoting a P9 recommendation is an audited governance mutation ─────────
def test_promote_recommendation_is_audited():
    rt, m = _run_to_gate()
    for _ in range(5):
        rt.learners.routing.observe("charge", "provA", ok=False)
    for _ in range(5):
        rt.learners.routing.observe("charge", "provB", ok=True)
    assert rt.learners.promoted_choice("routing", "charge") is None
    rt.promote_recommendation("routing", "charge", actor="ml-lead")
    assert rt.learners.promoted_choice("routing", "charge") == "provB"
    promoted = [e for e in rt.store.for_mission("governance") if e.type == "RecommendationPromoted"]
    assert promoted and promoted[0].payload["actor"] == "ml-lead"


# ─── the console read-model aggregates the control surface ───────────────────
def test_governance_console_aggregates():
    rt, m = _run_to_gate()
    rt.change_policy(m.id, actor="admin", reason="widen", grant=["c:write"])
    console = rt.governance_console()
    row = next(r for r in console["missions"] if r["id"] == m.id)
    assert row["policy_version"] == 2 and "c:write" in row["grants"]
    assert len(console["approval_queue"]) == 1          # b is still awaiting approval
    assert "recommendations" in console
