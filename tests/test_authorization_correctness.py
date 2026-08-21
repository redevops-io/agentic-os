"""An approval must authorize *this* invocation, not merely exist.

Approval systems usually prove the first half — that a human said yes — and
leave the second implicit: that the yes applies to the capability, the scope
and the plan actually about to run. The negative cases are where that gap
lives, so they are most of this file.

    an approval authorizes the node it named            positive control
    it does not authorize a different node              scope
    it does not survive the capability changing         identity
    it does not survive a policy tightening             freshness
    it does not survive a re-plan                       currency

A gate that passes all of the positives and none of these is a gate that
records consent without binding it to anything.
"""
from __future__ import annotations

import pytest

from agentic_os.mission.compiler import _node_id, compile_intent
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState,
)


def _spec(name, op, provides, **kw):
    return CapabilityManifest(op, [CapabilitySpec(name, op, provides=provides, **kw)])


class _Planner:
    def __init__(self, steps):
        self._steps = steps

    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=self._steps())


def _gated_mission(capability="pay.invoice", operator="pay"):
    """One side-effecting, approval-required node."""
    registry = CapabilityRegistry()
    registry.register(_spec(capability, operator, ["paid"],
                            side_effecting=True, approval_required=True))
    client = InMemoryOperatorClient({capability: lambda i: {"paid": True}})
    steps = lambda: [IntentStep(outcome="paid", need="pay the invoice")]
    runtime = MissionRuntime(registry, Executor(client), planner=_Planner(steps))
    mission = runtime.create_mission("pay it", policy_refs=["*"])
    runtime.run(mission.id)
    return runtime, mission, registry, client


class TestAnApprovalAuthorizesWhatItNamed:
    def test_the_gate_holds_until_someone_approves(self):
        runtime, mission, _, _ = _gated_mission()
        assert mission.state == MissionState.WAITING_HUMAN

    def test_approving_the_pending_node_lets_it_run(self):
        """The positive control. Without it the rest could pass on a runtime
        that never executes anything."""
        runtime, mission, _, _ = _gated_mission()
        pending = runtime.repo.pending_human(mission.id)
        runtime.approve(mission.id, pending["node_id"], "approve")
        assert runtime._missions[mission.id].state == MissionState.SUCCEEDED


class TestAnApprovalIsScopedToItsNode:
    def test_approving_a_different_node_does_not_release_the_gate(self):
        runtime, mission, _, _ = _gated_mission()
        runtime.approve(mission.id, "nd_somethingelse_1_paid", "approve")
        assert runtime._missions[mission.id].state != MissionState.SUCCEEDED


class TestAnApprovalIsBoundToTheCapabilityItApproved:
    """The case this file exists for.

    `ApprovalGranted` records a bare `node_id`, and `_node_id` is derived from
    `(mission, revision, outcome)` — the capability is not in it. So an
    approval granted for one capability satisfying an outcome also satisfies
    any *other* capability later bound to the same outcome at the same
    revision.

    That is not hypothetical: capability binding is a Context Runtime decision
    made per compile, and `plan_select` chooses among candidate plans. A
    provider swap is exactly the situation where a human's yes should stop
    applying, because what they approved is not what would run.
    """

    def test_a_rebound_outcome_does_not_inherit_the_approval(self):
        runtime, mission, registry, _ = _gated_mission(
            capability="pay.invoice", operator="pay")
        pending = runtime.repo.pending_human(mission.id)
        approved_node = pending["node_id"]
        runtime.approve(mission.id, approved_node, "approve")

        # The same outcome, a different capability, the same revision — so the
        # same node id. Revision lives on the plan, not the mission.
        revision = runtime._plans[mission.id].revision
        rebound = _node_id(mission.id, revision, "paid")
        assert rebound == approved_node, (
            "the node id already differs, so this defect does not arise "
            "and this test should be deleted rather than weakened")

        approvals = [e for e in runtime.store.for_mission(mission.id)
                     if e.type == "ApprovalGranted"]
        assert approvals, "nothing was approved"
        assert approvals[-1].payload.get("capability") == "pay.invoice", (
            "the approval must record what was approved, not only where")

        # Now rebind the outcome to a different capability at the same
        # revision, which is what a provider swap or a plan switch does. The
        # node id is unchanged, so a bare-node-id approval would carry.
        plan = runtime._plans[mission.id]
        for node in plan.graph.nodes:
            if node.id == approved_node:
                object.__setattr__(node, "capability", "pay.wire_transfer") \
                    if hasattr(node, "__dataclass_fields__") and \
                    getattr(node, "__dataclass_params__", None) and \
                    node.__dataclass_params__.frozen else \
                    setattr(node, "capability", "pay.wire_transfer")

        assert approved_node not in runtime._approved(mission.id, plan), (
            "a yes for pay.invoice still authorises pay.wire_transfer; what "
            "the human approved is not what would run")

    def test_an_unchanged_capability_keeps_its_approval(self):
        """The discriminating half. A rule that dropped every approval would
        pass the test above and make the gate unusable."""
        runtime, mission, _, _ = _gated_mission()
        pending = runtime.repo.pending_human(mission.id)
        runtime.approve(mission.id, pending["node_id"], "approve")
        plan = runtime._plans[mission.id]
        assert pending["node_id"] in runtime._approved(mission.id, plan)

    def test_a_legacy_approval_without_a_capability_is_grandfathered(self):
        """Failing closed on approvals recorded before the capability was
        stored would park missions that have already run and succeeded —
        changing the outcome of history on replay, which is a worse fault than
        the one it closes."""
        runtime, mission, _, _ = _gated_mission()
        pending = runtime.repo.pending_human(mission.id)
        runtime.store.append("ApprovalGranted", mission.id,
                             {"node_id": pending["node_id"], "edit": None})
        plan = runtime._plans[mission.id]
        assert pending["node_id"] in runtime._approved(mission.id, plan)


class TestAnApprovalDoesNotOutliveItsGrounds:
    def test_a_policy_change_invalidates_it(self):
        """Already implemented via `ApprovalInvalidated`; asserted so a later
        refactor cannot quietly drop it."""
        runtime, mission, _, _ = _gated_mission()
        pending = runtime.repo.pending_human(mission.id)
        runtime.approve(mission.id, pending["node_id"], "approve")
        assert pending["node_id"] in runtime._approved(mission.id)

        runtime.store.append("ApprovalInvalidated", mission.id,
                             {"node_id": pending["node_id"]})
        assert pending["node_id"] not in runtime._approved(mission.id)

    def test_a_new_revision_produces_a_node_the_old_approval_does_not_cover(self):
        """Currency. Node ids embed the revision, so a re-planned mission does
        not inherit consent given for the plan it replaced."""
        runtime, mission, _, _ = _gated_mission()
        revision = runtime._plans[mission.id].revision
        assert _node_id(mission.id, revision, "paid") != \
            _node_id(mission.id, revision + 1, "paid")
