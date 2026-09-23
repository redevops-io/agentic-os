"""Decision Intelligence — the PUBLIC interface boundary.

The public governed Runtime answers *what contracts does a decision Runtime obey?* It does **not** ship the
proprietary answer to *how does ReDevOps turn verified operational experience into better future decisions?*
— that lives behind this interface, in the private ``decision-intelligence`` package.

Dependency direction is fixed: the public Runtime depends only on this protocol; a private implementation
depends on the public contracts, never the reverse. The community can run the substrate with the no-op or a
simple deterministic implementation; the commercial Decision Intelligence plugs in the learning engine.

This module also states, domain-neutrally, the transition / experience-eligibility invariants that are a
trust signal rather than a moat — a decision Runtime must honour them regardless of who supplies the
intelligence:

    * a *proposal* is not an *execution* — proposing an action changes nothing on its own;
    * a *rejected* or *failed* action is not a *state transition* — it must not evolve state;
    * only a verified, executed outcome is *eligible* to become Experience.

Anyone can inspect these here; the value is in what a learner does *within* them, which is private.
"""
from __future__ import annotations

import enum
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


class Admissibility(str, enum.Enum):
    """Whether an intended action may take effect (the public transition contract)."""
    ADMITTED = "admitted"
    REJECTED = "rejected"                     # not approved → no execution, no state change
    INADMISSIBLE_STATE = "inadmissible_state"
    INADMISSIBLE_ACTION = "inadmissible_action"


def experience_eligible(*, approved: bool, admissible: bool, executed: bool, verified: bool) -> bool:
    """The public experience-eligibility invariant (trust signal, not mechanism): an outcome may become
    Experience only if the action was approved, admissible, actually executed, and independently verified.
    A rejected / inadmissible / unexecuted / unverified action is never eligible — so no learner (public or
    private) can learn from actions that never really happened or were never confirmed."""
    return bool(approved and admissible and executed and verified)


@runtime_checkable
class DecisionIntelligence(Protocol):
    """The seam between the public Runtime and a (private) decision-learning engine.

    Implementations decide *how* proposals are formed and *how* verified outcomes update future decisions.
    The Runtime treats a proposal as advisory: governance, admissibility and verification remain the
    Runtime's job (and are public). ``context`` / ``proposal`` / ``outcome`` / ``lesson`` are opaque to the
    Runtime — their shapes are the implementation's concern.
    """

    def propose(self, context: Mapping[str, Any]) -> Any:
        """Propose an action for a decision context. Advisory only; never authoritative over governance."""
        ...

    def observe_outcome(self, context: Mapping[str, Any], action: Any, outcome: Mapping[str, Any]) -> None:
        """Record a verified outcome so future decisions can improve. No-op for non-learning implementations."""
        ...

    def applicable_lessons(self, context: Mapping[str, Any]) -> Sequence[Any]:
        """Return the lessons (if any) an implementation considers applicable + sufficiently supported here.
        Advisory and inspectable; the Runtime does not turn a lesson into authority."""
        ...


class NoLearningDecisionIntelligence:
    """Default community implementation: no experience, no lessons. The Runtime is fully functional without
    any decision-learning engine — the OSS stack is not a hollow SDK."""

    def propose(self, context: Mapping[str, Any]) -> Any:
        return None

    def observe_outcome(self, context: Mapping[str, Any], action: Any, outcome: Mapping[str, Any]) -> None:
        return None

    def applicable_lessons(self, context: Mapping[str, Any]) -> Sequence[Any]:
        return ()


class DeterministicDecisionIntelligence:
    """A transparent, non-learning implementation driven by a caller-supplied deterministic policy. Useful as
    a baseline / example / control arm; it never adapts from outcomes."""

    def __init__(self, policy: Callable[[Mapping[str, Any]], Any]):
        self._policy = policy

    def propose(self, context: Mapping[str, Any]) -> Any:
        return self._policy(context)

    def observe_outcome(self, context: Mapping[str, Any], action: Any, outcome: Mapping[str, Any]) -> None:
        return None

    def applicable_lessons(self, context: Mapping[str, Any]) -> Sequence[Any]:
        return ()


__all__ = [
    "DecisionIntelligence", "NoLearningDecisionIntelligence", "DeterministicDecisionIntelligence",
    "Admissibility", "experience_eligible",
]
