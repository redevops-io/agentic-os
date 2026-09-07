"""Diagnostic Evidence Planner + safety gate — the ReDevOps differentiators (plan §6a/§6c).

The planner decides the *next* evidence to acquire — the cheapest, highest-information-gain question
or observation that isn't already answered — instead of dumping a generic questionnaire on the driver.
The safety gate ensures the product never tells a driver "you're fine" while a safety-critical cause is
still plausible. Both are pure functions over the domain contracts, so they're deterministic and tested
without a model or network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .contracts import (
    DiagnosticEvidenceRequest,
    EvidenceType,
    Hypothesis,
    Severity,
    Urgency,
)

# Confidence a hypothesis needs before we'd act on it without more evidence.
DEFAULT_CONFIDENCE_BAR = 0.7
# A safety-critical cause at or above this bar can't be ignored — it must surface even as a long shot.
SAFETY_PLAUSIBLE_BAR = 0.15


@dataclass(frozen=True)
class SafetyAssessment:
    passed: bool                 # True = safe to present a normal diagnosis (no un-cleared safety risk)
    stop_driving: bool
    advice: str


def safety_gate(hypotheses: Sequence[Hypothesis], *, plausible_bar: float = SAFETY_PLAUSIBLE_BAR) -> SafetyAssessment:
    """The hard guard: if any safety-critical hypothesis is even plausible, we never clear the vehicle —
    we surface it and advise caution. A wrong 'you're fine' is far costlier than an over-escalation."""
    critical = [h for h in hypotheses if h.is_safety_critical and float(h.confidence) >= plausible_bar]
    if not critical:
        return SafetyAssessment(passed=True, stop_driving=False, advice="")
    top = max(critical, key=lambda h: float(h.confidence))
    stop = (top.urgency is Urgency.STOP_DRIVING or top.severity is Severity.CRITICAL
            or float(top.confidence) >= DEFAULT_CONFIDENCE_BAR)
    advice = (f"A safety-critical issue is possible ({top.cause}). "
              + ("Stop driving and get it inspected before continuing."
                 if stop else "Have it checked before further driving."))
    return SafetyAssessment(passed=False, stop_driving=stop, advice=advice)


def is_confident(hypotheses: Sequence[Hypothesis], *, bar: float = DEFAULT_CONFIDENCE_BAR) -> bool:
    """Are we confident enough in a single top cause to stop asking for evidence?"""
    top = max((float(h.confidence) for h in hypotheses), default=0.0)
    return top >= bar


def plan_next_evidence(candidates: Iterable[DiagnosticEvidenceRequest], *,
                       already_have: Sequence[EvidenceType] = (),
                       safety_first: bool = True) -> Optional[DiagnosticEvidenceRequest]:
    """Pick the single best next request: highest expected value per unit of burden, skipping evidence
    types already collected and anything unavailable. If ``safety_first`` and any candidate flags a
    safety risk that resolving would reduce, it is preferred over a marginally higher-value ask."""
    have = set(already_have)
    pool = [c for c in candidates if c.availability and c.type not in have]
    if not pool:
        return None
    if safety_first:
        safety = [c for c in pool if c.safety_risk not in ("", "none")]
        if safety:
            return max(safety, key=lambda c: c.value_per_burden)
    return max(pool, key=lambda c: c.value_per_burden)


def rank_evidence(candidates: Iterable[DiagnosticEvidenceRequest], *,
                  already_have: Sequence[EvidenceType] = ()) -> List[DiagnosticEvidenceRequest]:
    """Full ordering (value ÷ burden, descending) of the still-useful requests — for EXPLAIN/telemetry."""
    have = set(already_have)
    pool = [c for c in candidates if c.availability and c.type not in have]
    return sorted(pool, key=lambda c: c.value_per_burden, reverse=True)


def should_stop_collecting(hypotheses: Sequence[Hypothesis],
                           candidates: Iterable[DiagnosticEvidenceRequest], *,
                           already_have: Sequence[EvidenceType] = (),
                           bar: float = DEFAULT_CONFIDENCE_BAR) -> Tuple[bool, Optional[DiagnosticEvidenceRequest]]:
    """Decide whether the Mission has enough to answer: stop if confident OR nothing useful is left to
    ask. Returns (stop, next_request) — the next_request is None when stopping."""
    if is_confident(hypotheses, bar=bar):
        return True, None
    nxt = plan_next_evidence(candidates, already_have=already_have)
    return (nxt is None), nxt
