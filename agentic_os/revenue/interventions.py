"""Leakage → governed intervention queue (Revenue & Execution plan §6, §17, §19).

The leakage detectors surface *recoverable revenue*; the Priority Engine decides what to *do* about each,
under one governance stance. This module composes the two into the pre-mission approval queue: every detected
`RevenueLeakage` is lifted to a Priority-Engine `InterventionCandidate` (`from_leakage`) and run through
`decide`, so each becomes an `InterventionDecision` — act, park on a human approval gate, or abstain — ranked
by transparent priority. Recovering leaked revenue is a state-changing / outbound action, so every candidate
is CONSEQUENTIAL and parks on approval; nothing here executes anything.

This is the hand-off point an app (e.g. the revenue agent) calls to turn a resolved `IntelligenceResult`'s
leakage into governed, approval-gated work: `plan_leakage_interventions(leaks)` → the ranked queue, then the
Mission Runtime materializes an approved decision into a mission. Pure + deterministic.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from agentic_os.priority_engine import (
    Action, InterventionDecision, PriorityPolicy, decide,
)
from agentic_os.revenue.leakage import RevenueLeakage, from_leakage


def plan_leakage_interventions(leakages: Iterable[RevenueLeakage], *, policy: Optional[PriorityPolicy] = None,
                               source_app: str = "revenue",
                               include_abstained: bool = False) -> List[InterventionDecision]:
    """Turn detected leakage into ranked, governed intervention decisions.

    Each leakage → `from_leakage` → `decide(policy)`; abstentions are dropped (unless `include_abstained`),
    and the survivors are ordered by priority (highest first) — the queue a human works top-down.
    """
    decisions: List[InterventionDecision] = []
    for leak in leakages:
        d = decide(from_leakage(leak, source_app=source_app), policy)
        if d.action is Action.ABSTAIN and not include_abstained:
            continue
        decisions.append(d)
    decisions.sort(key=lambda d: d.priority.total, reverse=True)
    return decisions


def decision_view(d: InterventionDecision) -> Dict[str, Any]:
    """A serializable view of one decision — what an app surfaces in its approval queue / API."""
    c = d.candidate
    return {
        "subject": c.subject,
        "action": d.action.value,
        "requires_approval": d.requires_approval,
        "priority": round(d.priority.total, 4),
        "risk_adjusted_value": round(d.priority.risk_adjusted_value, 4),
        "proposed_action": c.proposed_action,
        "required_capabilities": list(c.required_capabilities),
        "risk_tier": c.risk_tier.name,
        "rationale": d.rationale,
        "candidate_id": c.candidate_id,
        "action_kind": c.action_kind,
        "observation_refs": list(c.observation_refs),
    }


def intervention_queue(leakages: Iterable[RevenueLeakage], *, policy: Optional[PriorityPolicy] = None,
                       source_app: str = "revenue") -> Dict[str, Any]:
    """The full governed queue as a serializable dict: ranked decisions + a summary. Ready for an endpoint."""
    decisions = plan_leakage_interventions(leakages, policy=policy, source_app=source_app)
    views = [decision_view(d) for d in decisions]
    approvals = sum(1 for d in decisions if d.requires_approval)
    return {
        "count": len(views),
        "requires_approval": approvals,
        "auto": len(views) - approvals,
        "decisions": views,
    }
