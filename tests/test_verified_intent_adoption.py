"""Mission plans from a sealed intent, and cannot reach the sentence.

The restated invariant is not "one LLM call at the top". It is:

    no LLM may change mission semantics after a VerifiedIntent is sealed.

`planner.py` has asserted that in prose since the boundary was described. This
file is what makes it a property of the code, because until now `agentic-os`
imported nothing from the contracts and its entry point took a goal *string*
that `TemplatePlanner._match` keyword-matched to decide what the mission was.

The strongest evidence here is structural rather than behavioural.
`VerifiedIntent` carries `utterance_ref` and never the utterance, so a runtime
handed one cannot re-read the sentence even by mistake. The tests below check
the two directions that would expose it if that were untrue: corrupt the goal
and the plan must not move; change the intent and it must.
"""
from __future__ import annotations

import pytest
from runtime_contracts import Author, IntentField, OpenReason, Unresolved, VerifiedIntent

from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.from_intent import (
    OBJECTIVE_TEMPLATES, UnsealedIntent, UnsupportedObjective,
)
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec


def _spec(name, op, provides, **kw):
    return CapabilityManifest(op, [CapabilitySpec(name, op, provides=provides, **kw)])


def _registry():
    registry = CapabilityRegistry()
    for name, outcome in (("billing.lookup", "overdue_invoice"),
                          ("comms.dunning", "dunning_sent"),
                          ("billing.record", "payment_recorded")):
        registry.register(_spec(name, name.split(".")[0], [outcome],
                                permissions=["billing:write"]))
    return registry


def _runtime(store):
    client = InMemoryOperatorClient({})
    return MissionRuntime(_registry(), Executor(client), store=store)


def intent(*, objective="recover_overdue_payment", sealed=True, blocking=False,
           **fields) -> VerifiedIntent:
    base = {"customer": "acme", "amount": "1200"}
    base.update(fields)
    draft = VerifiedIntent(
        objective=objective,
        produced_by="discovery-runtime@0.4.2",
        utterance_ref="utt-77",
        fields={k: IntentField(value=v, author=Author.USER)
                for k, v in base.items()},
        unresolved=(Unresolved(dimension="channel", reason=OpenReason.READERS_DISAGREED,
                               result_changing=True),) if blocking else ())
    return draft.seal() if sealed and not blocking else draft


GRANT = ["billing:write"]


class TestWhatItRefuses:
    def test_a_draft_is_not_planned(self):
        """The seal is the whole point: an unsealed intent is one whose meaning
        Discovery has not closed, and planning it executes a guess."""
        with pytest.raises(UnsealedIntent):
            _runtime(EventStore()).create_mission_from_intent(
                intent(sealed=False), policy_refs=GRANT)

    def test_an_objective_with_no_lifecycle_is_refused_by_name(self):
        """Not approximated. `TemplatePlanner` falls back to a single outcome
        slugged from the goal, which is right for a human typing a sentence and
        wrong here — it would turn "this runtime cannot do that" into a mission
        that runs and answers a different question."""
        with pytest.raises(UnsupportedObjective) as raised:
            _runtime(EventStore()).create_mission_from_intent(
                intent(objective="reconcile_payroll"), policy_refs=GRANT)
        assert "reconcile_payroll" in str(raised.value)

    def test_nothing_is_recorded_for_a_refused_intent(self):
        """A mission created and then blocked leaves a log entry for a request
        that was never admissible."""
        store = EventStore()
        with pytest.raises(UnsupportedObjective):
            _runtime(store).create_mission_from_intent(
                intent(objective="reconcile_payroll"), policy_refs=GRANT)
        assert store.all() == [], (
            "the refusal happened after something was already written, so the "
            "log now describes a request that was never admissible")


class TestTheSentenceIsUnreachable:
    def test_the_artifact_does_not_carry_the_utterance(self):
        """The structural half, and the one that does not rely on any code
        path being written carefully. There is nothing to re-read."""
        stored = intent().to_json()
        flat = repr(stored)
        assert "utt-77" in flat, "the reference is kept"
        assert "chase" not in flat and "overdue invoice" not in flat.lower(), (
            "the artifact must reference the utterance, never contain it")

    def test_corrupting_the_goal_does_not_change_the_plan(self, monkeypatch):
        """`goal` is a label. If replay moves when it changes, the sentence is
        still load-bearing and this adoption is cosmetic."""
        store = EventStore()
        mission = _runtime(store).create_mission_from_intent(
            intent(), policy_refs=GRANT)
        before = next(e for e in store.for_mission(mission.id)
                      if e.type == "PlanCreated").payload["signature"]

        fresh = _runtime(store)
        monkeypatch.setattr(fresh.repo, "goal", lambda _id: "onboard a new customer")
        restored = fresh.rehydrate(mission.id)
        assert restored.template == "invoice_recovery"
        assert fresh._plans[mission.id] is not None
        assert fresh._signature(fresh._plans[mission.id]) == before

    def test_a_different_intent_does_change_the_plan(self):
        """The discriminating opposite. Without it, "the plan never moves"
        would pass by the plan never depending on anything."""
        first = _runtime(EventStore()).create_mission_from_intent(
            intent(), policy_refs=GRANT)
        second = _runtime(EventStore()).create_mission_from_intent(
            intent(objective="onboard_customer"), policy_refs=["*"])
        assert first.template != second.template


class TestTheLogCanReplanFromTheIntent:
    def test_mission_created_records_the_intent_and_its_hash(self):
        store = EventStore()
        source = intent()
        mission = _runtime(store).create_mission_from_intent(source, policy_refs=GRANT)
        payload = next(e for e in store.for_mission(mission.id)
                       if e.type == "MissionCreated").payload
        assert payload["intent_hash"] == source.intent_hash
        assert payload["intent_produced_by"] == "discovery-runtime@0.4.2"
        assert payload["utterance_ref"] == "utt-77"

    def test_the_whole_artifact_is_stored_not_only_the_hash(self):
        """A hash proves the intent has not changed and cannot reconstruct it.
        A log holding only the hash forces replay back to the goal string."""
        store = EventStore()
        source = intent()
        mission = _runtime(store).create_mission_from_intent(source, policy_refs=GRANT)
        payload = next(e for e in store.for_mission(mission.id)
                       if e.type == "MissionCreated").payload

        from runtime_contracts import intent_from_json
        restored = intent_from_json(payload["intent"])
        assert restored.intent_hash == source.intent_hash
        assert restored.is_verified, "it must come back sealed, or replay reseals a draft"

    def test_the_verified_fields_reach_the_nodes(self):
        """Adoption that dropped the fields would pass every test above while
        planning a template with no idea what it was about — the objective
        would be the only thing the intent contributed."""
        runtime = _runtime(EventStore())
        mission = runtime.create_mission_from_intent(
            intent(customer="globex"), policy_refs=GRANT)
        nodes = runtime._plans[mission.id].graph.nodes
        assert nodes
        for node in nodes:
            assert node.inputs["intent"]["customer"] == "globex"

    def test_intent_fields_cannot_shadow_a_world_reference(self):
        """They share `node.inputs` with the compiler's `$from_world` wiring,
        which is written second. Flat, a field named after an upstream outcome
        would overwrite that reference silently."""
        runtime = _runtime(EventStore())
        mission = runtime.create_mission_from_intent(
            intent(overdue_invoice="INV-1"), policy_refs=GRANT)
        wired = [n for n in runtime._plans[mission.id].graph.nodes
                 if "overdue_invoice" in n.inputs]
        assert wired, "the template does wire this outcome, so the case is real"
        for node in wired:
            assert node.inputs["overdue_invoice"] == {"$from_world": "overdue_invoice"}
            assert node.inputs["intent"]["overdue_invoice"] == "INV-1"

    def test_a_goal_string_mission_gets_no_intent_inputs(self):
        """The discriminating opposite: the inputs come from the intent, not
        from the compiler inventing a key that is always there."""
        runtime = _runtime(EventStore())
        mission = runtime.create_mission("chase the overdue invoice",
                                         policy_refs=GRANT)
        for node in runtime._plans[mission.id].graph.nodes:
            assert "intent" not in node.inputs


class TestDataCannotSteerAuthority:
    def test_a_field_value_cannot_add_an_approval_gate(self):
        """The defect the first version of this shipped with.

        Verified fields were routed through `IntentStep.constraints`, and the
        compiler substring-matches constraints for "human" or "review" to set
        `approval_required`. So a customer named Review Corp would have added
        an approval gate nobody declared — user-supplied text steering an
        authority decision, inside the module written to prevent exactly that.
        """
        runtime = _runtime(EventStore())
        benign = runtime.create_mission_from_intent(intent(), policy_refs=GRANT)
        loaded = runtime.create_mission_from_intent(
            intent(customer="Review Corp (human resources)"), policy_refs=GRANT)

        def gates(mission):
            return [n.approval_required
                    for n in runtime._plans[mission.id].graph.nodes]

        assert gates(loaded) == gates(benign), (
            "a field value changed which nodes need approval")


class TestTheOldEntryPointStillWorks:
    def test_a_goal_string_mission_is_unaffected(self):
        """This is an addition, not a replacement. Missions created from prose
        keep working, and keep reading the prose — that is what they are."""
        store = EventStore()
        mission = _runtime(store).create_mission(
            "chase the overdue invoice", policy_refs=GRANT)
        assert mission.goal == "chase the overdue invoice"
        payload = next(e for e in store.for_mission(mission.id)
                       if e.type == "MissionCreated").payload
        assert "intent_hash" not in payload


def test_every_declared_objective_has_a_template_that_exists():
    """A table entry naming a template nobody wrote fails at plan time, in
    production, for one objective — the worst place to find it."""
    from agentic_os.mission import templates

    for objective, template in OBJECTIVE_TEMPLATES.items():
        assert templates.get(template, "m1") is not None, (
            f"{objective} maps to {template!r}, which is not a template")
