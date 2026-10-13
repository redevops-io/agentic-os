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
from typing import Callable, List, Optional, Sequence, Tuple

from agentic_os.observation import KnownAtQuality, Observation
from agentic_os.priority_engine import (
    DecisionOpportunity, PriorityPolicy, UtilityFn, canonical_action_label, normalize_action_label,
    select_action)


class LeakageError(Exception):
    """Raised when an observation used at a decision point was not knowable then (future leakage)."""


def is_knowable_at(o: Observation, decision_time: float) -> bool:
    return o.valid_at <= decision_time and o.known_at <= decision_time


def admissible_for_a0(o: Observation) -> bool:
    """A0 may only use observations whose ``known_at`` is defensible (OBSERVED or RECONSTRUCTED).
    UNKNOWN provenance FAILS CLOSED — it cannot enter a leakage-safe retrospective evaluation, so no
    one can later weaken the gate by feeding in snapshots with an inferred known_at."""
    return o.known_at_quality in (KnownAtQuality.OBSERVED, KnownAtQuality.RECONSTRUCTED)


def as_of(observations: Sequence[Observation], decision_time: float, *,
          subject: Optional[str] = None, require_quality: bool = True) -> List[Observation]:
    """The leakage-safe reconstruction: observations knowable at ``decision_time`` (optionally scoped to
    one ``subject``). This is the ONLY set a decision at T may be built from. With ``require_quality``
    (the A0 default), observations whose ``known_at`` provenance is UNKNOWN are excluded (fail-closed)."""
    return [o for o in observations
            if is_knowable_at(o, decision_time)
            and (subject is None or o.subject == subject)
            and (not require_quality or admissible_for_a0(o))]


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
    gate_violations: int           # points whose evidence tripped the leakage gate. 0 in the default
                                   # fail-closed mode (a violation RAISES instead); counted in 'count' mode
    leakage_details: Tuple[str, ...] = ()   # one message per counted violation ('count' mode only)

    @property
    def agreement_rate(self) -> float:
        return self.agreement / self.evaluated if self.evaluated else 0.0


# builder: given a subject and the AS-OF observations, produce the opportunity (or None if not enough).
OpportunityBuilder = Callable[[str, List[Observation]], Optional[DecisionOpportunity]]

#: leakage handling. "raise" (default, production, fail-closed): the first violation raises LeakageError.
#: "count": record the violation, skip that point, continue — for measuring how leaky a history is.
LEAKAGE_RAISE = "raise"
LEAKAGE_COUNT = "count"


def replay(observations, decision_points: Sequence[DecisionPoint],
           build_opportunity: OpportunityBuilder, *, policy: Optional[PriorityPolicy] = None,
           utility_fn: Optional[UtilityFn] = None, require_quality: bool = True,
           leakage: str = LEAKAGE_RAISE) -> ReplayReport:
    """Run the runtime over reconstructed history — THE one replay loop (``a0_evaluation.run_a0`` is a
    thin wrapper over this). ``observations`` is either a list of Observations or an object with
    ``as_of(decision_time, *, subject, require_quality)`` (an operational observation store). For each
    decision point: reconstruct the as-of evidence (A0-admissible only, by default), ENFORCE the leakage
    gate, build the opportunity from that evidence only, select an action, and compare to the expert's
    action under the canonical label (:func:`agentic_os.priority_engine.canonical_action_label`, so an
    abstention agrees with a human 'do nothing'). A builder that reaches past the as-of set trips the gate:
    ``leakage="raise"`` (default, fail-closed) raises :class:`LeakageError`; ``leakage="count"`` records the
    violation in ``gate_violations``, skips the point and continues."""
    if leakage not in (LEAKAGE_RAISE, LEAKAGE_COUNT):
        raise ValueError(f"leakage must be '{LEAKAGE_RAISE}' or '{LEAKAGE_COUNT}'")
    p = policy or PriorityPolicy()
    is_store = hasattr(observations, "as_of")
    evaluated = agreement = broken = violations = 0
    details: List[str] = []
    for dp in decision_points:
        try:
            if is_store:
                obs = observations.as_of(dp.decision_time, subject=dp.subject, require_quality=require_quality)
            else:
                obs = as_of(observations, dp.decision_time, subject=dp.subject, require_quality=require_quality)
            assert_no_leakage(obs, dp.decision_time)      # the as-of set must be clean by construction
            opp = build_opportunity(dp.subject, obs)
            if opp is None or not opp.candidate_actions:
                broken += 1
                continue
            # guard the builder too: nothing it cited as evidence may post-date / fall outside the as-of set
            cited = {ref for c in opp.candidate_actions for ref in c.observation_refs}
            if cited:
                as_of_ids = {o.observation_id for o in obs}
                if is_store:
                    outside = sorted(cited - as_of_ids)
                else:
                    outside = sorted(o.observation_id for o in observations
                                     if o.observation_id in cited and not is_knowable_at(o, dp.decision_time))
                if outside:
                    raise LeakageError(f"builder cited evidence outside the as-of set at T={dp.decision_time}: "
                                       f"{outside[:3]}")
        except LeakageError as e:
            if leakage == LEAKAGE_RAISE:
                raise
            violations += 1
            details.append(str(e))
            continue
        sel = select_action(opp, p, utility_fn=utility_fn)
        if dp.known_good_action is not None:
            evaluated += 1
            if canonical_action_label(sel.action) == normalize_action_label(dp.known_good_action):
                agreement += 1
    return ReplayReport(n_points=len(decision_points), evaluated=evaluated, agreement=agreement,
                        broken_projections=broken, gate_violations=violations, leakage_details=tuple(details))
