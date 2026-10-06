"""Deal closing conditions — the normalized, evidence-backed state of what a deal needs to close (Phase 1, §6).

A *condition* is a closing requirement ("the economic buyer is identified and engaged", "security review is
complete") in a methodology-neutral, normalized vocabulary. Its state is six-valued, extending the
entity-resolution plane's status model (:class:`agentic_os.integration.contracts.ResolutionStatus`) with PARTIAL
and STALE so the runtime can be HONEST: UNKNOWN (never assessed) is distinct from UNSATISFIED (assessed, not met),
and STALE (was satisfied, evidence now too old) is distinct from SATISFIED. A condition is never silently
promoted — conflicting evidence surfaces as CONFLICTED.

Which conditions a given deal must satisfy, and how they map to stages, is methodology — that is Phase 2. This
module owns only the condition *catalog* (the normalized names) and the deterministic *state derivation* from
evidence, which every methodology reuses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, Mapping, Tuple

from ..integrations.business.contracts import BusinessObject, now_ms


class ConditionState(str, Enum):
    """Six-valued, honest condition state. Extends ResolutionStatus (RESOLVED/PROBABLE/CONFLICTED/UNRESOLVED)
    with the closing-specific PARTIAL and STALE, and renames to the condition domain."""
    SATISFIED = "satisfied"        # met, with sufficient fresh evidence
    PARTIAL = "partial"            # some but not all required evidence present
    UNSATISFIED = "unsatisfied"    # assessed and demonstrably not met
    UNKNOWN = "unknown"            # never assessed / no evidence either way (NOT the same as unsatisfied)
    CONFLICTED = "conflicted"      # evidence disagrees — never auto-resolved
    STALE = "stale"                # previously satisfied, but the supporting evidence is now too old to trust


# The normalized condition vocabulary (§6: 22 conditions). Methodology-neutral; a methodology (Phase 2) selects
# a subset, orders them and maps them to stages. Names are stable identifiers, not display strings.
NORMALIZED_CONDITIONS: Tuple[str, ...] = (
    "BUSINESS_PROBLEM_CONFIRMED",
    "METRICS_QUANTIFIED",
    "ECONOMIC_BUYER_IDENTIFIED",
    "ECONOMIC_BUYER_ENGAGED",
    "CHAMPION_IDENTIFIED",
    "CHAMPION_ACTIVE",
    "DECISION_PROCESS_MAPPED",
    "DECISION_CRITERIA_KNOWN",
    "COMPETITION_UNDERSTOOD",
    "TECHNICAL_VALIDATION_DONE",
    "SECURITY_REVIEW_COMPLETE",
    "LEGAL_REVIEW_COMPLETE",
    "PROCUREMENT_ENGAGED",
    "PRICING_AGREED",
    "QUOTE_DELIVERED",
    "QUOTE_ACCEPTED",
    "BUDGET_CONFIRMED",
    "PAPER_PROCESS_KNOWN",
    "MUTUAL_PLAN_AGREED",
    "CONTRACT_SENT",
    "CONTRACT_REDLINES_RESOLVED",
    "SIGNATURE_PENDING",
)
_VALID = frozenset(NORMALIZED_CONDITIONS)

# Default freshness windows (ms) by condition. Fast-moving conditions (an engaged champion, a pending signature)
# go STALE quickly; a completed review stays trustworthy longer. A methodology may override these.
_DAY = 86_400_000
DEFAULT_MAX_AGE_MS: Mapping[str, int] = {
    "ECONOMIC_BUYER_ENGAGED": 21 * _DAY,
    "CHAMPION_ACTIVE": 14 * _DAY,
    "QUOTE_DELIVERED": 14 * _DAY,
    "SIGNATURE_PENDING": 7 * _DAY,
    "CONTRACT_SENT": 14 * _DAY,
}
_DEFAULT_MAX_AGE_MS = 60 * _DAY


@dataclass(frozen=True)
class DealCondition(BusinessObject):
    """One normalized closing condition and its evidence-backed state for a specific deal."""
    KIND: ClassVar[str] = "deal_closing.condition"
    deal_ref: str = ""
    name: str = ""                              # one of NORMALIZED_CONDITIONS
    state: ConditionState = ConditionState.UNKNOWN
    confidence: float = 0.0
    evidence_refs: Tuple[str, ...] = ()
    last_verified_ms: int = 0                   # 0 = never verified
    detail: str = ""

    def with_freshness(self, *, now: int | None = None, max_age_ms: int | None = None) -> "DealCondition":
        """Return a copy demoted to STALE if a SATISFIED/PARTIAL condition's evidence is older than its window.
        UNKNOWN/UNSATISFIED/CONFLICTED are unaffected (there is nothing fresh to expire)."""
        if self.state not in (ConditionState.SATISFIED, ConditionState.PARTIAL) or not self.last_verified_ms:
            return self
        window = max_age_ms if max_age_ms is not None else DEFAULT_MAX_AGE_MS.get(self.name, _DEFAULT_MAX_AGE_MS)
        ref = now if now is not None else now_ms()
        if ref - self.last_verified_ms > window:
            from dataclasses import replace
            return replace(self, state=ConditionState.STALE,
                           detail=(self.detail + " | " if self.detail else "") + "evidence past freshness window")
        return self


@dataclass(frozen=True)
class EvidenceSignal:
    """A minimal normalized input to condition derivation: does a piece of evidence support or contradict the
    condition, how strongly, and when was the underlying fact observed."""
    supports: bool
    confidence: float = 0.5
    observed_ms: int = 0
    ref: str = ""


def derive_state(signals: Tuple[EvidenceSignal, ...], *,
                 satisfy_threshold: float = 0.6, partial_threshold: float = 0.3) -> Tuple[ConditionState, float]:
    """Deterministically derive a condition state + confidence from evidence signals, honestly.

    - no signals            → UNKNOWN (never assessed — NOT unsatisfied)
    - supporting AND contradicting both present and material → CONFLICTED (never averaged away)
    - net support ≥ satisfy_threshold → SATISFIED
    - net support ≥ partial_threshold → PARTIAL
    - otherwise               → UNSATISFIED
    Confidence is the summed strength on the winning side, capped at 1.0.
    """
    if not signals:
        return ConditionState.UNKNOWN, 0.0
    support = sum(s.confidence for s in signals if s.supports)
    against = sum(s.confidence for s in signals if not s.supports)
    has_support = any(s.supports and s.confidence >= partial_threshold for s in signals)
    has_against = any((not s.supports) and s.confidence >= partial_threshold for s in signals)
    if has_support and has_against:
        return ConditionState.CONFLICTED, round(min(support, against), 4)
    net = support - against
    if net >= satisfy_threshold:
        return ConditionState.SATISFIED, round(min(support, 1.0), 4)
    if net >= partial_threshold:
        return ConditionState.PARTIAL, round(min(support, 1.0), 4)
    return ConditionState.UNSATISFIED, round(min(against, 1.0), 4)


def assess_condition(deal_ref: str, name: str, signals: Tuple[EvidenceSignal, ...], *,
                     now: int | None = None, provider: str = "deal_closing",
                     max_age_ms: int | None = None) -> DealCondition:
    """Build a freshness-adjusted :class:`DealCondition` from evidence signals. Raises on an unknown condition
    name so a typo can never masquerade as a silently-UNKNOWN condition."""
    if name not in _VALID:
        raise ValueError(f"unknown condition {name!r}; must be one of NORMALIZED_CONDITIONS")
    from ..integrations.business.contracts import Provenance
    state, confidence = derive_state(signals)
    refs = tuple(s.ref for s in signals if s.ref)
    last = max((s.observed_ms for s in signals), default=0) if state in (
        ConditionState.SATISFIED, ConditionState.PARTIAL) else 0
    cond = DealCondition(prov=Provenance(provider=provider, evidence_refs=refs),
                         deal_ref=deal_ref, name=name, state=state, confidence=confidence,
                         evidence_refs=refs, last_verified_ms=last)
    return cond.with_freshness(now=now, max_age_ms=max_age_ms)


__all__ = [
    "ConditionState", "DealCondition", "EvidenceSignal",
    "NORMALIZED_CONDITIONS", "DEFAULT_MAX_AGE_MS",
    "derive_state", "assess_condition",
]
