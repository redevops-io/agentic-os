"""Phase A0 — historical replay / retrospective evaluation, with a HARD leakage gate
(REAL_DATA_PROVIDER_SCOPING.md v2 §4/§4.1).

Before waiting days/weeks for live outcomes, replay existing history: reconstruct the evidence that was
knowable at a past decision time T, let the runtime choose an action *as of T*, and compare against the
outcome we now know followed. This won't establish causality, but it exposes broken projections,
leakage, bad features and obviously poor ranking in hours, not weeks.

The non-negotiable rule (§4.1): **every feature used to select an action must have been knowable at
decision time.** With the bi-temporal observation model that is a precise, enforceable gate:

    an observation is usable at decision_time T  iff  valid_at <= T  AND  known_at <= T

`as_of(...)` reconstructs exactly that set; `assert_no_leakage(...)` is the hard gate (it raises); and
`audit_leakage(...)` reports violations without raising. Skipping this gate is how retrospective
benchmarks accidentally become "extremely impressive and completely invalid."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from agentic_os.observation import Observation
from agentic_os.priority_engine import (
    DecisionOpportunity, PriorityPolicy, UtilityFn, select_action)


class LeakageError(Exception):
    """Raised when an observation used at a decision point was not knowable then (future leakage)."""


def is_knowable_at(o: Observation, decision_time: float) -> bool:
    return o.valid_at <= decision_time and o.known_at <= decision_time


def as_of(observations: Sequence[Observation], decision_time: float, *,
          subject: Optional[str] = None) -> List[Observation]:
    """The leakage-safe reconstruction: observations knowable at ``decision_time`` (optionally scoped to
    one ``subject``). This is the ONLY set a decision at T may be built from."""
    return [o for o in observations
            if is_knowable_at(o, decision_time) and (subject is None or o.subject == subject)]


def audit_leakage(observations: Sequence[Observation], decision_time: float) -> List[Observation]:
    """Non-raising: return the observations that would leak future information at ``decision_time``."""
    return [o for o in observations if not is_knowable_at(o, decision_time)]


def assert_no_leakage(observations: Sequence[Observation], decision_time: float) -> None:
    """The hard gate. Raises :class:`LeakageError` if any observation was not knowable at
    ``decision_time`` (either it became true, or we learned it, only later)."""
    bad = audit_leakage(observations, decision_time)
    if bad:
        o = bad[0]
        raise LeakageError(
            f"{len(bad)} observation(s) leak future info at decision_time={decision_time}: "
            f"e.g. {o.observation_id!r} (valid_at={o.valid_at}, known_at={o.known_at})")


@dataclass(frozen=True)
class DecisionPoint:
    decision_time: float
    subject: str
    known_good_action: Optional[str] = None   # the action a human/expert took (the counterfactual label)
    broke_out: Optional[bool] = None           # a known later binary outcome, if available


@dataclass(frozen=True)
class ReplayReport:
    n_points: int
    evaluated: int                 # points with a known_good_action to compare against
    agreement: int                 # runtime chose the same action as the expert
    broken_projections: int        # points where the as-of state produced no candidate actions
    gate_violations: int           # must be 0 — every decision was built from knowable evidence only

    @property
    def agreement_rate(self) -> float:
        return self.agreement / self.evaluated if self.evaluated else 0.0


# builder: given a subject and the AS-OF observations, produce the opportunity (or None if not enough).
OpportunityBuilder = Callable[[str, List[Observation]], Optional[DecisionOpportunity]]


def replay(observations: Sequence[Observation], decision_points: Sequence[DecisionPoint],
           build_opportunity: OpportunityBuilder, *, policy: Optional[PriorityPolicy] = None,
           utility_fn: Optional[UtilityFn] = None) -> ReplayReport:
    """Run the runtime over reconstructed history. For each decision point: reconstruct the as-of
    evidence, ENFORCE the leakage gate, build the opportunity from that evidence only, select an action,
    and compare to the expert's action. A builder that reaches past the as-of set trips the gate."""
    p = policy or PriorityPolicy()
    evaluated = agreement = broken = 0
    for dp in decision_points:
        obs = as_of(observations, dp.decision_time, subject=dp.subject)
        assert_no_leakage(obs, dp.decision_time)          # the as-of set must be clean by construction
        opp = build_opportunity(dp.subject, obs)
        if opp is None or not opp.candidate_actions:
            broken += 1
            continue
        # guard the builder too: nothing it cited as evidence may post-date the decision
        cited = {ref for c in opp.candidate_actions for ref in c.observation_refs}
        if cited:
            leaked = [o for o in observations if o.observation_id in cited and not is_knowable_at(o, dp.decision_time)]
            if leaked:
                raise LeakageError(f"builder cited future evidence at T={dp.decision_time}: "
                                   f"{leaked[0].observation_id!r}")
        sel = select_action(opp, p, utility_fn=utility_fn)
        if dp.known_good_action is not None:
            evaluated += 1
            if sel.action.action_kind == dp.known_good_action:
                agreement += 1
    return ReplayReport(n_points=len(decision_points), evaluated=evaluated, agreement=agreement,
                        broken_projections=broken, gate_violations=0)
