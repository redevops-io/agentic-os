"""Replay must reconstruct the program that ran, or say that it cannot.

`rehydrate` re-derives the plan by calling the planner again and compiling the
result. That is the durability design and it is sound — the graph is
deterministic, so recomputing beats storing it. But "deterministic" is a
property of the code, and the code is what changes between the run and the
restart. A keyword table gains a word, a template gains a step, a capability's
cost model is retuned; the recompiled graph is now a different program wearing
the original's id, folding the original's execution events.

Nothing checked. `PlanCreated` has recorded a `signature` — the capability
chain — since the kernel was written, and `rehydrate` never read it. The
mission came back, the state folded, the log stayed plausible, and the program
was not the one the log described.

Two changes, and they close different halves:

    the plan is compared to the recorded signature   detects divergence
    the resolved template is recorded and reused     removes one cause of it

The second is the more interesting one. `TemplatePlanner._match` keyword-matches
the goal *prose* to choose a template, and `MissionCreated` recorded the
template the caller passed — `None` — rather than the one the match produced. So
the prose was re-read on every restart, and the match was re-decided against
whatever the keyword table said today. Recording the resolved template is what
makes restart stop reading the sentence, which is the same invariant the
Discovery boundary asks for, arriving here early and for its own reasons.
"""
from __future__ import annotations

import pytest

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.planner import TemplatePlanner
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime, ReplayDivergence
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep,
)


def _spec(name, op, provides, **kw):
    return CapabilityManifest(op, [CapabilitySpec(name, op, provides=provides, **kw)])


def _registry():
    registry = CapabilityRegistry()
    registry.register(_spec("billing.charge", "billing", ["charged"],
                            permissions=["billing:write"]))
    registry.register(_spec("billing.refund", "billing", ["refunded"],
                            permissions=["billing:write"]))
    # Enough to satisfy the `invoice_recovery` template, so the prose tests
    # exercise the real planner against a real graph rather than a stub.
    for name, outcome in (("billing.lookup", "overdue_invoice"),
                          ("comms.dunning", "dunning_sent"),
                          ("billing.record", "payment_recorded")):
        registry.register(_spec(name, name.split(".")[0], [outcome],
                                permissions=["billing:write"]))
    return registry


class _Planner:
    """A planner whose output can be changed between the run and the restart —
    which is what a deploy does."""

    def __init__(self, steps):
        self.steps = steps

    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=list(self.steps))


def _runtime(store, planner):
    client = InMemoryOperatorClient({
        "billing.charge": lambda i: {"charged": True},
        "billing.refund": lambda i: {"refunded": True}})
    return MissionRuntime(_registry(), Executor(client), store=store,
                          planner=planner)


CHARGE = [IntentStep(outcome="charged", need="charge the customer")]
REFUND = [IntentStep(outcome="refunded", need="refund the customer")]

GRANT = ["billing:write"]


class TestReplayRefusesToSubstituteAnotherProgram:
    def test_an_unchanged_deployment_replays(self):
        """The control. Without it the fix could be "rehydrate always raises",
        which would pass every other test in this file."""
        store = EventStore()
        planner = _Planner(CHARGE)
        mission = _runtime(store, planner).create_mission("charge", policy_refs=GRANT)

        restored = _runtime(store, _Planner(CHARGE)).rehydrate(mission.id)
        assert restored.id == mission.id

    def test_a_changed_plan_is_refused_and_not_silently_adopted(self):
        """The defect. A planner that produces a different graph after a
        restart gets the original's id and the original's execution events."""
        store = EventStore()
        mission = _runtime(store, _Planner(CHARGE)).create_mission(
            "charge", policy_refs=GRANT)

        with pytest.raises(ReplayDivergence) as raised:
            _runtime(store, _Planner(REFUND)).rehydrate(mission.id)

        message = str(raised.value)
        assert "billing.charge" in message and "billing.refund" in message, (
            "the refusal must name both programs; 'replay diverged' without "
            "them leaves the reader unable to tell which one was real")

    def test_the_recorded_signature_is_what_it_compares_against(self):
        """Not the in-memory plan the first runtime happens to still hold.
        Comparing against a live object would pass on the same process and
        prove nothing about a restart, which is the only case that matters."""
        store = EventStore()
        mission = _runtime(store, _Planner(CHARGE)).create_mission(
            "charge", policy_refs=GRANT)
        recorded = next(e for e in store.for_mission(mission.id)
                        if e.type == "PlanCreated").payload["signature"]

        fresh = _runtime(store, _Planner(REFUND))
        assert mission.id not in fresh._plans, "a genuinely fresh runtime"
        with pytest.raises(ReplayDivergence) as raised:
            fresh.rehydrate(mission.id)
        assert recorded in str(raised.value)


class TestRestartDoesNotRereadTheProse:
    """`TemplatePlanner` chooses by keyword-matching the goal. Whatever that is
    at creation, a restart must not decide it again."""

    def _mission_with_matched_template(self, store):
        runtime = _runtime(store, TemplatePlanner())
        # No template passed: the planner matches "overdue invoice" itself.
        return runtime.create_mission("chase the overdue invoice", policy_refs=["*"])

    def test_the_resolved_template_is_recorded_not_the_one_passed(self):
        """It belongs on `PlanCreated`, not `MissionCreated`: a re-plan can
        resolve differently, so the template is a property of the plan. The
        caller's argument stays on `MissionCreated`, where it is accurate — it
        is the request, and the request was `None`."""
        store = EventStore()
        mission = self._mission_with_matched_template(store)
        created = next(e for e in store.for_mission(mission.id)
                       if e.type == "MissionCreated")
        plan = next(e for e in store.for_mission(mission.id)
                    if e.type == "PlanCreated")
        assert created.payload["template"] is None
        assert plan.payload["template"] == "invoice_recovery", (
            "the log recorded only the template the caller passed, so the "
            "prose is the sole record of the choice and has to be re-read to "
            "recover it")

    def test_a_changed_keyword_table_does_not_change_a_restart(self, monkeypatch):
        """The discriminating mutation. Same goal, same intent, different
        reader — and replay must be unmoved, because the choice was already
        made and recorded."""
        store = EventStore()
        mission = self._mission_with_matched_template(store)

        monkeypatch.setattr(TemplatePlanner, "_KEYWORDS",
                            {"onboarding": ["invoice", "overdue"]})
        restored = _runtime(store, TemplatePlanner()).rehydrate(mission.id)
        assert restored.template == "invoice_recovery"

    def test_the_goal_prose_is_not_what_replay_reads(self, monkeypatch):
        """The other direction: corrupt the stored prose and replay is
        unaffected. If this fails, the sentence is still load-bearing below the
        boundary and `VerifiedIntent` adoption would be cosmetic."""
        store = EventStore()
        mission = self._mission_with_matched_template(store)

        fresh = _runtime(store, TemplatePlanner())
        monkeypatch.setattr(fresh.repo, "goal", lambda _id: "onboard a new customer")
        assert fresh.rehydrate(mission.id).template == "invoice_recovery"
