"""Property 2: the log must reconstruct not just what happened, but why it was allowed.

    original runtime -> append-only log -> fresh runtime -> rehydrate()

A replay that reaches the same terminal state under a *different* authority has
not reproduced the mission. It has reconstructed a more permissive one that
happened to end up in the same place, and the next thing it does may not be
something the original was ever permitted to do.

That was the actual state here. `MissionCreated` recorded goal, template and
constraints — never `policy_refs` — so `rehydrate` passed `["*"]` because it
was the only thing it could pass. Since `compile_intent` fails closed on a
missing grant, a mission refused under a restrictive policy would compile and
run after a restart, with nothing in the log marking the change.
"""
from __future__ import annotations

import pytest

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime, UnrecoverableAuthority
from agentic_os.mission.store import EventStore
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


def _registry():
    registry = CapabilityRegistry()
    registry.register(_spec("billing.charge", "billing", ["charged"],
                            permissions=["billing:write"]))
    return registry


def _runtime(store, **kw):
    client = InMemoryOperatorClient({"billing.charge": lambda i: {"charged": True}})
    steps = lambda: [IntentStep(outcome="charged", need="charge the customer")]
    return MissionRuntime(_registry(), Executor(client), store=store,
                          planner=_Planner(steps), **kw)


def _ran_under(policy_refs):
    """A mission created and run under a named policy, and its log."""
    store = EventStore()
    runtime = _runtime(store)
    mission = runtime.create_mission("charge them", policy_refs=policy_refs)
    runtime.run(mission.id)
    return store, runtime, mission


class TestTheAuthorityIsInTheLog:
    def test_mission_created_records_the_policy(self):
        """Without this the log says what happened and cannot say why it was
        allowed, and rehydrate has nothing to restore."""
        store, _, mission = _ran_under(["billing:write"])
        created = next(e for e in store.for_mission(mission.id)
                       if e.type == "MissionCreated")
        assert created.payload["policy_refs"] == ["billing:write"]


class TestReplayRestoresTheAuthorityExactly:
    def test_a_restrictive_policy_is_not_widened(self):
        """The case that matters. Replay must not turn a specific grant into a
        wildcard — that is not a provenance mismatch, it is a change of
        authorization."""
        store, _, mission = _ran_under(["billing:write"])
        fresh = _runtime(store)
        restored = fresh.rehydrate(mission.id)
        assert restored.policy_refs == ["billing:write"]
        assert "*" not in restored.policy_refs

    def test_a_wildcard_mission_still_replays_as_wildcard(self):
        """The discriminating opposite, so the fix does not become "wildcards
        are forbidden" — a mission genuinely created with one must keep it."""
        store, _, mission = _ran_under(["*"])
        assert _runtime(store).rehydrate(mission.id).policy_refs == ["*"]

    def test_the_mission_identity_and_terminal_state_survive(self):
        store, original, mission = _ran_under(["billing:write"])
        restored = _runtime(store).rehydrate(mission.id)
        assert restored.id == mission.id
        assert restored.state == original._missions[mission.id].state

    def test_the_same_capability_is_bound_to_each_node(self):
        """Same plan, not merely the same outcome. A replay that reached the
        same state through a different capability reproduced a coincidence."""
        store, original, mission = _ran_under(["billing:write"])
        before = {n.id: n.capability
                  for n in original._plans[mission.id].graph.nodes}
        fresh = _runtime(store)
        fresh.rehydrate(mission.id)
        after = {n.id: n.capability for n in fresh._plans[mission.id].graph.nodes}
        assert before == after


class TestAnUnrecordedAuthorityIsRefusedNotInvented:
    def test_a_log_predating_policy_recording_raises(self):
        """A wildcard is not a conservative default. It is the most permissive
        answer available, applied exactly where nobody recorded one."""
        store, _, mission = _ran_under(["billing:write"])
        for event in store.for_mission(mission.id):
            if event.type == "MissionCreated":
                event.payload.pop("policy_refs")
        with pytest.raises(UnrecoverableAuthority) as raised:
            _runtime(store).rehydrate(mission.id)
        assert "policy_refs" in str(raised.value)

    def test_a_caller_may_supply_them_explicitly(self):
        """What the old comment said production would do — from the permissions
        plane, by a caller that holds them, rather than guessed here."""
        store, _, mission = _ran_under(["billing:write"])
        for event in store.for_mission(mission.id):
            if event.type == "MissionCreated":
                event.payload.pop("policy_refs")
        restored = _runtime(store).rehydrate(mission.id,
                                             policy_refs=["billing:write"])
        assert restored.policy_refs == ["billing:write"]


class TestPropertiesTwoAndThreeMeetAtRestart:
    """An in-memory authorization fix that does not survive a restart is a fix
    to the running system and not to the persisted one."""

    def test_a_rebound_capability_is_still_unapproved_after_rehydration(self):
        registry = CapabilityRegistry()
        registry.register(_spec("pay.invoice", "pay", ["paid"],
                                side_effecting=True, approval_required=True))
        client = InMemoryOperatorClient({"pay.invoice": lambda i: {"paid": True}})
        steps = lambda: [IntentStep(outcome="paid", need="pay it")]

        store = EventStore()
        original = MissionRuntime(registry, Executor(client), store=store,
                                  planner=_Planner(steps))
        mission = original.create_mission("pay it", policy_refs=["*"])
        original.run(mission.id)
        pending = original.repo.pending_human(mission.id)
        original.approve(mission.id, pending["node_id"], "approve")

        fresh = MissionRuntime(registry, Executor(client), store=store,
                               planner=_Planner(steps))
        fresh.rehydrate(mission.id)
        plan = fresh._plans[mission.id]
        for node in plan.graph.nodes:
            if node.id == pending["node_id"]:
                setattr(node, "capability", "pay.wire_transfer")

        assert pending["node_id"] not in fresh._approved(mission.id, plan), (
            "the approval survived a restart and a capability change, so the "
            "persisted system authorises what the running one refuses")

    def test_an_unscoped_approval_is_marked_rather_than_equated(self):
        """Historical replay is preserved without pretending the old event
        carried information it never recorded."""
        registry = CapabilityRegistry()
        registry.register(_spec("pay.invoice", "pay", ["paid"],
                                side_effecting=True, approval_required=True))
        client = InMemoryOperatorClient({"pay.invoice": lambda i: {"paid": True}})
        steps = lambda: [IntentStep(outcome="paid", need="pay it")]
        store = EventStore()
        runtime = MissionRuntime(registry, Executor(client), store=store,
                                 planner=_Planner(steps))
        mission = runtime.create_mission("pay it", policy_refs=["*"])
        runtime.run(mission.id)
        node_id = runtime.repo.pending_human(mission.id)["node_id"]

        store.append("ApprovalGranted", mission.id,
                     {"node_id": node_id, "edit": None})
        assert runtime.approval_scope(mission.id, node_id) == \
            MissionRuntime.LEGACY_UNSCOPED_APPROVAL

        runtime.approve(mission.id, node_id, "approve")
        assert runtime.approval_scope(mission.id, node_id) in (
            "SCOPED", MissionRuntime.LEGACY_UNSCOPED_APPROVAL)
