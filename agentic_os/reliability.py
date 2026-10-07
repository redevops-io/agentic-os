"""Workflow Reliability — a generic expected-transition abstraction (plan §17, P6).

The audit found the Obligation plane gives a reusable *end-state* verification + exception taxonomy, but there is
no GENERIC ``ExpectedTransition``/``ReliabilityIncident`` abstraction, and the plan says reliability belongs in the
Runtime, not one domain agent. This is that abstraction: declare that a subject should move ``from_state`` →
``expected_to_state`` by a deadline, observe, and raise a typed incident when the transition does not occur
(overdue) or diverges into a failure state. One loop serves OrderReliability / DealReliability / QuoteReliability /
RevenueReliability / IntegrationReliability / ServiceReliability / CampaignReliability.

Pure + deterministic; it complements (does not replace) the Obligation engine's read-after-write verification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple


class TransitionStatus(str, Enum):
    PENDING = "pending"          # within window, not yet observed in the target state
    OCCURRED = "occurred"        # observed in the expected target state
    OVERDUE = "overdue"          # deadline passed, target state not observed
    FAILED = "failed"            # observed in a declared failure state
    RESOLVED = "resolved"        # an incident was remediated and the transition then occurred


class FailureType(str, Enum):
    NOT_OCCURRED = "not_occurred"            # expected state never observed by the deadline
    DIVERGED = "diverged"                    # observed in a declared failure state
    CONFLICTING_STATE = "conflicting_state"  # observed a state inconsistent with the expected one


@dataclass(frozen=True)
class ExpectedTransition:
    """A subject should move from_state → expected_to_state by ``expected_by`` (ms). ``failure_states`` are states
    that mean it went wrong; ``remediation_policy`` names what to do if it doesn't happen."""
    subject: str
    expected_to_state: str
    from_state: str = ""
    expected_by: int = 0                      # deadline in ms; 0 = no deadline
    evidence_required: Tuple[str, ...] = ()
    failure_states: Tuple[str, ...] = ()
    remediation_policy: str = ""
    impact: float = 0.0


@dataclass(frozen=True)
class ReliabilityIncident:
    """A transition that did not occur as expected (§17). Candidate remediations are proposed, authority-gated."""
    transition: ExpectedTransition
    observed_state: str
    failure_type: FailureType
    status: TransitionStatus
    impact: float = 0.0
    candidate_remediations: Tuple[str, ...] = ()
    authority: str = ""


def evaluate_transition(transition: ExpectedTransition, *, observed_state: str, now: int) -> Optional[ReliabilityIncident]:
    """Return a ReliabilityIncident if the transition has gone wrong, else None. Deterministic:
    - observed == expected_to_state → occurred (None);
    - observed in failure_states → DIVERGED incident (FAILED);
    - deadline passed and not in target → NOT_OCCURRED incident (OVERDUE);
    - otherwise still pending (None)."""
    if observed_state == transition.expected_to_state:
        return None
    remediations = (transition.remediation_policy,) if transition.remediation_policy else ()
    if observed_state and observed_state in transition.failure_states:
        return ReliabilityIncident(transition=transition, observed_state=observed_state,
                                   failure_type=FailureType.DIVERGED, status=TransitionStatus.FAILED,
                                   impact=transition.impact, candidate_remediations=remediations)
    if transition.expected_by and now > transition.expected_by:
        return ReliabilityIncident(transition=transition, observed_state=observed_state,
                                   failure_type=FailureType.NOT_OCCURRED, status=TransitionStatus.OVERDUE,
                                   impact=transition.impact, candidate_remediations=remediations)
    return None


def monitor_transitions(transitions: Sequence[ExpectedTransition], *, observed: Mapping[str, str],
                        now: int) -> Tuple[ReliabilityIncident, ...]:
    """Evaluate many expected transitions against observed states; return the incidents (empty = all healthy).
    Deterministic order: by subject."""
    incidents = []
    for t in sorted(transitions, key=lambda t: t.subject):
        inc = evaluate_transition(t, observed_state=observed.get(t.subject, ""), now=now)
        if inc is not None:
            incidents.append(inc)
    return tuple(incidents)


__all__ = [
    "TransitionStatus", "FailureType", "ExpectedTransition", "ReliabilityIncident",
    "evaluate_transition", "monitor_transitions",
]
