"""Rehydrating a FINISHED mission restores its outcome (not just its state): the same `{"success", "world"}` the
live run produced, so a resumed process can read results without re-deriving them from the log."""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.sdk import step, template
from agentic_os.mission.store import EventStore
from agentic_os.mission.registry import CapabilityRegistry


@template("rehydrate_outcome_fixture")
def _fixture(mission_id):
    return [step("asked", need="ask"),
            step("answered", need="collect the answer", after=["asked"]),
            step("applied", need="apply", after=["answered"])]


def _ops(fail_apply=False):
    def apply(i):
        if fail_apply:
            raise RuntimeError("apply exploded")
        return {"ok": i["answered"]["a"] == "4"}
    return [Operator("fx", [
        capability("q.ask", handler=lambda i: {"q": "2+2?"}, provides=["asked"]),
        capability("q.collect", handler=lambda i: {"a": (i.get("_approval") or {}).get("a")}, provides=["answered"],
                   approval_required=True),
        capability("q.apply", handler=apply, provides=["applied"]),
    ])]


def _runtime(ops, path):
    reg = CapabilityRegistry()
    for op in ops:
        reg.register(op.manifest)
    return MissionRuntime(reg, Executor(LocalOperatorClient({o.name: o for o in ops})), store=EventStore(path))


def _finish(tmp_path, fail_apply=False):
    path = str(tmp_path / "ledger.jsonl")
    rt = _runtime(_ops(fail_apply), path)
    m = rt.create_mission("answer", policy_refs=[], template="rehydrate_outcome_fixture")
    m = rt.run(m.id)
    (task,) = [t for t in rt.inbox() if t["mission_id"] == m.id]
    live = rt.approve(m.id, task["node_id"], "approve", edit={"a": "4"})
    fresh = _runtime(_ops(fail_apply), path)                 # "restart": same log, nothing in memory
    return live, fresh.rehydrate(m.id)


def test_succeeded_mission_outcome_is_restored(tmp_path):
    live, back = _finish(tmp_path)
    assert live.state.value == back.state.value == "succeeded"
    assert back.outcome == live.outcome and back.outcome["world"]["applied"] == {"ok": True}


def test_failed_mission_outcome_is_restored(tmp_path):
    live, back = _finish(tmp_path, fail_apply=True)
    assert back.state.value == live.state.value == "failed"
    assert back.outcome["success"] is False and back.outcome["reason"]


def test_parked_mission_has_no_outcome(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    rt = _runtime(_ops(), path)
    m = rt.run(rt.create_mission("answer", policy_refs=[], template="rehydrate_outcome_fixture").id)
    assert _runtime(_ops(), path).rehydrate(m.id).outcome is None
