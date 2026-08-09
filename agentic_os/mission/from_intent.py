"""A mission from a sealed `VerifiedIntent`, without ever seeing the sentence.

`create_mission(goal: str, …)` is how this runtime has always started. With a
Discovery Runtime above it that entry point is wrong in a specific way: it hands
Mission a sentence and lets it interpret. `TemplatePlanner._match` then decides
what the mission *is* by keyword-matching that string — a prose reader below the
boundary, which is the same shape as the regex compiler the Quantify migration
exists to delete.

This module is the other entry. It takes an intent whose meaning Discovery has
already closed, and it cannot read prose even if it wanted to: `VerifiedIntent`
carries `utterance_ref`, never the utterance. That is the property doing the
work here — not a rule anyone has to remember, but an artifact that does not
contain the thing it must not consult.

    VerifiedIntent ──► OBJECTIVE_TEMPLATES ──► ExecutionIntent ──► compile
                   └─► UnsupportedObjective   (never a generic fallback)

**Two refusals, both deliberate.**

An unsealed intent is refused. A draft is one whose meaning is still open, and
planning it would execute a guess — the seal exists precisely so a consumer need
not judge that for itself.

An objective with no declared template is refused *by name*. `TemplatePlanner`
falls back to a single outcome slugged from the goal, which is the right
behaviour for a human typing a sentence and the wrong one here: a fallback would
turn "Discovery meant something this runtime cannot do" into a plan that runs and
answers a different question. The manifest boundary is that refusals are named,
never approximated.
"""
from __future__ import annotations

from typing import Any, Mapping

from runtime_contracts import VerifiedIntent

from .types import ExecutionIntent, IntentStep

#: Which lifecycle an objective means, declared rather than inferred.
#:
#: A table and not a matcher. Discovery's objective vocabulary is closed, so the
#: binding is a lookup; the moment it becomes a search over strings, this
#: runtime is interpreting again.
OBJECTIVE_TEMPLATES: Mapping[str, str] = {
    "onboard_customer": "onboarding",
    "recover_overdue_payment": "invoice_recovery",
}


class UnsupportedObjective(ValueError):
    """Discovery meant something this runtime has no lifecycle for.

    Raised rather than approximated. `MissionOutcome` in the contracts has
    `UNSUPPORTED_CAPABILITY` and deliberately no `REINTERPRETED`, and this is
    the same refusal one layer down.
    """

    def __init__(self, objective: str) -> None:
        self.objective = objective
        super().__init__(
            f"no lifecycle is declared for objective {objective!r}. Add one to "
            "OBJECTIVE_TEMPLATES, or refuse — planning a generic single-outcome "
            "mission would run something the intent never asked for")


class UnsealedIntent(ValueError):
    """Discovery has not closed this intent's meaning, so it is not executable."""


def check_executable(intent: VerifiedIntent) -> None:
    """The two conditions that must hold before anything is planned."""
    if not intent.is_verified:
        raise UnsealedIntent(
            "the intent is a draft; Discovery has not sealed it, so its meaning "
            "is still open and planning it would execute a guess")
    open_dimensions = sorted(u.dimension for u in intent.blocking)
    if open_dimensions:
        raise UnsealedIntent(
            f"sealed, but still blocked on {', '.join(open_dimensions)} — "
            "readers disagreed and it was not settled")


def template_for(intent: VerifiedIntent) -> str:
    check_executable(intent)
    try:
        return OBJECTIVE_TEMPLATES[intent.objective]
    except KeyError:
        raise UnsupportedObjective(intent.objective) from None


class IntentPlanner:
    """A planner whose input is the intent, and whose `goal` argument is inert.

    It keeps the `Planner` shape — `plan(mission_id, goal, context)` — because
    the runtime calls planners that way, and takes the intent through the
    context. The `goal` parameter is accepted and never read; a test corrupts it
    to prove that.
    """

    def __init__(self, inner: Any = None) -> None:
        #: Where the steps come from once the template is chosen. Injected so a
        #: caller can supply its own catalogue; defaults to the shipped one.
        self._inner = inner

    def plan(self, mission_id: str, goal: str, context: dict) -> ExecutionIntent:
        intent = (context or {}).get("verified_intent")
        if intent is None:
            raise UnsealedIntent(
                "IntentPlanner was given no verified intent. It has no other "
                "input — falling back to the goal string is exactly what this "
                "planner exists to prevent")

        template = template_for(intent)
        from . import templates

        planned = self._inner.get(template, mission_id) if self._inner is not None \
            else templates.get(template, mission_id)
        if planned is None:
            raise UnsupportedObjective(intent.objective)

        planned.template = template
        planned.rationale = (
            f"objective {intent.objective!r} -> template {template!r} "
            f"(intent {intent.intent_hash})")
        return _with_intent_inputs(planned, intent)


def _with_intent_inputs(planned: ExecutionIntent, intent: VerifiedIntent) -> ExecutionIntent:
    """Carry the verified fields on the intent, for the compiler to place.

    **Not through `constraints`,** which was the first version of this and is a
    trap. The compiler decides `approval_required` by substring-matching each
    constraint for "human" or "review", so a verified field whose *value*
    happened to contain either — a customer called Review Corp, a note
    mentioning human resources — would silently add an approval gate that
    nobody declared. User-supplied text steering an authority decision is the
    inversion this whole boundary exists to prevent, and it would have arrived
    inside the module built to enforce it.

    So the fields travel as data on a field of their own, and
    `test_a_field_value_cannot_add_an_approval_gate` holds that line.
    """
    planned.verified_fields = {name: str(field.value)
                               for name, field in sorted(intent.fields.items())}
    return planned


def mission_record(intent: VerifiedIntent) -> dict:
    """What `MissionCreated` must carry so a restart can plan from the intent.

    The whole artifact, not only its hash. A hash proves the intent has not
    changed and cannot reconstruct it, so a log holding only the hash forces
    replay back to the goal string — the thing this module exists to stop
    reading.
    """
    return {"intent_hash": intent.intent_hash,
            "intent_produced_by": intent.produced_by,
            "utterance_ref": intent.utterance_ref,
            "intent": intent.to_json()}
