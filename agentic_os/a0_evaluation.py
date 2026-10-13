"""Phase A0 over the operational store — leakage-safe retrospective evaluation (PR-sequence.odt PR 5).

`historical_replay` established the mechanism over an in-memory observation list. A0 in a real
deployment runs the same discipline over the OPERATIONAL store: reconstruct the evidence knowable at a
past decision time (the store's own `as_of`, which is leakage-safe and quality-gated), let the runtime
choose an action *as of* T, and compare against what we now know the expert did. It exposes broken
projections, leakage, bad features and poor ranking in hours — it does NOT establish causality (the odt
is explicit about that), so the primary metric is **agreement with the expert's later action**, not
lift.

Two guards keep the retrospective honest:
  * observations come only from ``store.as_of(T, require_quality=True)`` — future / UNKNOWN-provenance
    evidence can't enter (fail-closed);
  * a builder that cites an observation id NOT in the as-of set trips a :class:`LeakageError`.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from agentic_os.historical_replay import (
    DecisionPoint, LeakageError, OpportunityBuilder, ReplayReport, assert_no_leakage, replay)
from agentic_os.intervention_record import ActorType
from agentic_os.priority_engine import PriorityPolicy, UtilityFn, normalize_action_label


class ObservationSource:
    """Anything with an ``as_of(decision_time, *, subject, require_quality)`` — an in-memory or a
    Postgres observation store."""
    def as_of(self, decision_time: float, *, subject=None, require_quality=True): ...  # pragma: no cover


def _subject_of(opportunity_id: str) -> str:
    """Default subject extractor: 'outreach:Acme' → 'Acme' (fall back to the whole id)."""
    return opportunity_id.split(":", 1)[1] if ":" in opportunity_id else opportunity_id


def decision_points_from_interventions(intervention_store, *,
                                       subject_of: Callable[[str], str] = _subject_of
                                       ) -> List[DecisionPoint]:
    """Reconstruct A0 decision points from recorded history: each RUNTIME intervention is a decision at
    its ``proposed_at``; its label is the HUMAN action recorded for the SAME opportunity (if any). That
    yields the agreement question — 'as of T the runtime chose X; the expert later did Y; did they
    agree?' — over exactly the opportunities where a human also acted."""
    human_by_opp = {}
    for r in intervention_store.all():
        if r.actor_type == ActorType.HUMAN:
            human_by_opp.setdefault(r.opportunity_id, r.selected_action)
    points: List[DecisionPoint] = []
    for r in intervention_store.all():
        if r.actor_type != ActorType.RUNTIME:
            continue
        points.append(DecisionPoint(decision_time=r.proposed_at, subject=subject_of(r.opportunity_id),
                                    known_good_action=human_by_opp.get(r.opportunity_id)))
    return points


def run_a0(observation_store: ObservationSource, decision_points: Sequence[DecisionPoint],
           build_opportunity: OpportunityBuilder, *, policy: Optional[PriorityPolicy] = None,
           utility_fn: Optional[UtilityFn] = None, require_quality: bool = True,
           leakage: str = "raise") -> ReplayReport:
    """Run leakage-safe A0 against a store: a thin wrapper over :func:`agentic_os.historical_replay.replay`
    (the single replay loop), which reconstructs the as-of evidence from the store (leakage-safe +
    A0-admissible by default), builds the opportunity from ONLY that evidence, selects an action, and
    compares to the expert's later action. A builder citing evidence outside the as-of set trips a
    LeakageError (``leakage="count"`` counts instead of raising)."""
    return replay(observation_store, decision_points, build_opportunity, policy=policy,
                  utility_fn=utility_fn, require_quality=require_quality, leakage=leakage)
