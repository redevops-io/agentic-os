"""Blocker taxonomy + inference (Phase 3, plan §10-§11).

A methodology readiness assessment (:mod:`agentic_os.deal_closing.methodology`) tells us WHICH conditions are
blocking, conflicted, stale or inconsistent. This module turns those raw state signals into first-class
**Blockers** — a normalized taxonomy of *why a deal is stuck* — each carrying the evidence for and against it, an
impact/urgency/resolvability read, and the candidate intervention kinds that might resolve it.

Crucially, inference stops at the blocker: it does NOT jump from a missing condition straight to an action. The
leap from "this is the likely cause" to "do this" is a hypothesis that must carry contradicting evidence and a
verification step (:mod:`agentic_os.deal_closing.hypotheses`), mirroring the discovery plane's
``DeterministicHypothesisPlanner`` ("promote only if supporting outweighs contradicting").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, Mapping, Tuple

from ..integrations.business.contracts import BusinessObject, Provenance
from .conditions import ConditionState
from .methodology import ClosingMethodology, MethodologyReadiness


class InterventionKind(str, Enum):
    """Normalized candidate action kinds a blocker may suggest. NO_ACTION is always a candidate; the full
    CandidateAction model + ActionScore (Phase 4) scores these against do-nothing."""
    NO_ACTION = "no_action"
    VERIFY_CLAIM = "verify_claim"                 # re-read the system of record (resolves UNKNOWN/STALE)
    RESOLVE_CONFLICT = "resolve_conflict"         # reconcile disagreeing sources (never auto-merge)
    QUALIFY_DISCOVERY = "qualify_discovery"
    IDENTIFY_STAKEHOLDER = "identify_stakeholder"
    ENGAGE_ECONOMIC_BUYER = "engage_economic_buyer"
    STRENGTHEN_CHAMPION = "strengthen_champion"
    MAP_DECISION_PROCESS = "map_decision_process"
    CLARIFY_CRITERIA = "clarify_criteria"
    COMPETITIVE_DIFFERENTIATION = "competitive_differentiation"
    REQUEST_TECHNICAL_VALIDATION = "request_technical_validation"
    REQUEST_SECURITY_REVIEW = "request_security_review"
    ADVANCE_LEGAL_REVIEW = "advance_legal_review"
    ENGAGE_PROCUREMENT = "engage_procurement"
    AGREE_PRICING = "agree_pricing"
    SEND_QUOTE = "send_quote"
    FOLLOW_UP_QUOTE = "follow_up_quote"
    CONFIRM_BUDGET = "confirm_budget"
    PREPARE_MUTUAL_PLAN = "prepare_mutual_plan"
    SEND_CONTRACT = "send_contract"
    ADVANCE_SIGNATURE = "advance_signature"
    ESCALATE = "escalate"


class BlockerType(str, Enum):
    """The 23-value normalized blocker taxonomy (§10)."""
    DISCOVERY_GAP = "discovery_gap"
    METRICS_UNQUANTIFIED = "metrics_unquantified"
    STAKEHOLDER_GAP = "stakeholder_gap"
    STAKEHOLDER_CONFLICT = "stakeholder_conflict"
    CHAMPION_WEAK = "champion_weak"
    NO_ECONOMIC_BUYER_ACCESS = "no_economic_buyer_access"
    DECISION_PROCESS_UNKNOWN = "decision_process_unknown"
    DECISION_CRITERIA_UNKNOWN = "decision_criteria_unknown"
    COMPETITIVE_THREAT = "competitive_threat"
    TECHNICAL_BLOCKER = "technical_blocker"
    SECURITY_BLOCKER = "security_blocker"
    LEGAL_BLOCKER = "legal_blocker"
    PROCUREMENT_BLOCKER = "procurement_blocker"
    PRICING_GAP = "pricing_gap"
    QUOTE_STALLED = "quote_stalled"
    BUDGET_UNCONFIRMED = "budget_unconfirmed"
    PAPER_PROCESS_UNKNOWN = "paper_process_unknown"
    NO_MUTUAL_PLAN = "no_mutual_plan"
    CONTRACT_STALLED = "contract_stalled"
    EVIDENCE_CONFLICT = "evidence_conflict"
    STALE_EVIDENCE = "stale_evidence"
    PROCESS_INCONSISTENCY = "process_inconsistency"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Blocker(BusinessObject):
    """An inferred reason a deal is stuck, with evidence both ways and a resolvability read. Never an action —
    the action is proposed by a hypothesis that must survive verification first."""
    KIND: ClassVar[str] = "deal_closing.blocker"
    deal_ref: str = ""
    blocker_type: BlockerType = BlockerType.UNKNOWN
    subject: str = ""                             # the condition/role the blocker concerns
    detail: str = ""
    confidence: float = 0.0                       # how sure we are the blocker is real
    impact: float = 0.0                           # 0..1, how much it threatens the close
    urgency: float = 0.0                          # 0..1, how time-sensitive
    resolvability: float = 0.0                    # 0..1, how addressable it is
    supporting_evidence: Tuple[str, ...] = ()
    contradicting_evidence: Tuple[str, ...] = ()
    candidate_interventions: Tuple[InterventionKind, ...] = ()


# (condition family → blocker type + candidate interventions) — the config table the inference reads, in the
# style of discovery/proposals._TEMPLATE_HINTS. Each entry says: if THIS condition is the substantive problem
# (UNSATISFIED/PARTIAL), it is THIS kind of blocker, and THESE interventions might resolve it.
_K = InterventionKind
_CONDITION_BLOCKER: Mapping[str, Tuple[BlockerType, Tuple[InterventionKind, ...]]] = {
    "BUSINESS_PROBLEM_CONFIRMED": (BlockerType.DISCOVERY_GAP, (_K.QUALIFY_DISCOVERY,)),
    "METRICS_QUANTIFIED": (BlockerType.METRICS_UNQUANTIFIED, (_K.QUALIFY_DISCOVERY,)),
    "CHAMPION_IDENTIFIED": (BlockerType.STAKEHOLDER_GAP, (_K.IDENTIFY_STAKEHOLDER,)),
    "CHAMPION_ACTIVE": (BlockerType.CHAMPION_WEAK, (_K.STRENGTHEN_CHAMPION,)),
    "ECONOMIC_BUYER_IDENTIFIED": (BlockerType.STAKEHOLDER_GAP, (_K.IDENTIFY_STAKEHOLDER,)),
    "ECONOMIC_BUYER_ENGAGED": (BlockerType.NO_ECONOMIC_BUYER_ACCESS,
                               (_K.ENGAGE_ECONOMIC_BUYER, _K.STRENGTHEN_CHAMPION)),
    "DECISION_PROCESS_MAPPED": (BlockerType.DECISION_PROCESS_UNKNOWN, (_K.MAP_DECISION_PROCESS,)),
    "DECISION_CRITERIA_KNOWN": (BlockerType.DECISION_CRITERIA_UNKNOWN, (_K.CLARIFY_CRITERIA,)),
    "COMPETITION_UNDERSTOOD": (BlockerType.COMPETITIVE_THREAT, (_K.COMPETITIVE_DIFFERENTIATION,)),
    "TECHNICAL_VALIDATION_DONE": (BlockerType.TECHNICAL_BLOCKER, (_K.REQUEST_TECHNICAL_VALIDATION,)),
    "SECURITY_REVIEW_COMPLETE": (BlockerType.SECURITY_BLOCKER, (_K.REQUEST_SECURITY_REVIEW,)),
    "LEGAL_REVIEW_COMPLETE": (BlockerType.LEGAL_BLOCKER, (_K.ADVANCE_LEGAL_REVIEW,)),
    "PROCUREMENT_ENGAGED": (BlockerType.PROCUREMENT_BLOCKER, (_K.ENGAGE_PROCUREMENT,)),
    "PRICING_AGREED": (BlockerType.PRICING_GAP, (_K.AGREE_PRICING,)),
    "QUOTE_DELIVERED": (BlockerType.QUOTE_STALLED, (_K.SEND_QUOTE,)),
    "QUOTE_ACCEPTED": (BlockerType.QUOTE_STALLED, (_K.FOLLOW_UP_QUOTE,)),
    "BUDGET_CONFIRMED": (BlockerType.BUDGET_UNCONFIRMED, (_K.CONFIRM_BUDGET,)),
    "PAPER_PROCESS_KNOWN": (BlockerType.PAPER_PROCESS_UNKNOWN, (_K.MAP_DECISION_PROCESS,)),
    "MUTUAL_PLAN_AGREED": (BlockerType.NO_MUTUAL_PLAN, (_K.PREPARE_MUTUAL_PLAN,)),
    "CONTRACT_SENT": (BlockerType.CONTRACT_STALLED, (_K.SEND_CONTRACT,)),
    "CONTRACT_REDLINES_RESOLVED": (BlockerType.CONTRACT_STALLED, (_K.ADVANCE_LEGAL_REVIEW, _K.ESCALATE)),
    "SIGNATURE_PENDING": (BlockerType.CONTRACT_STALLED, (_K.ADVANCE_SIGNATURE, _K.ESCALATE)),
}
_DEFAULT_BLOCKER = (BlockerType.UNKNOWN, (_K.VERIFY_CLAIM,))


def _stage_impact(methodology: ClosingMethodology, condition: str) -> float:
    """Later-stage conditions threaten a closer deal more. Impact rises toward the final stage."""
    spec = methodology.spec(condition)
    if spec is None or spec.stage not in methodology.stages or len(methodology.stages) < 2:
        return 0.6
    idx = methodology.stages.index(spec.stage)
    return round(0.5 + 0.5 * (idx / (len(methodology.stages) - 1)), 4)


def infer_blockers(methodology: ClosingMethodology, readiness: MethodologyReadiness,
                   states: Mapping[str, ConditionState], *, deal_ref: str = "",
                   provider: str = "deal_closing") -> Tuple[Blocker, ...]:
    """Deterministically infer blockers from a readiness assessment. Each produces a Blocker with evidence both
    ways; the interventions are CANDIDATES only (NO_ACTION always included), never a committed action.

    - CONFLICTED condition → EVIDENCE_CONFLICT (resolve, never auto-merge)
    - STALE condition      → STALE_EVIDENCE (re-verify)
    - inconsistency        → PROCESS_INCONSISTENCY (verify the unmet dependency)
    - UNKNOWN blocking     → DISCOVERY_GAP (we don't know yet — distinct from a substantive blocker)
    - UNSATISFIED blocking → the substantive blocker from the condition family
    """
    out: list[Blocker] = []

    def emit(btype: BlockerType, subject: str, detail: str, *, confidence: float, support: Tuple[str, ...],
             against: Tuple[str, ...], interventions: Tuple[InterventionKind, ...], resolvability: float,
             urgency: float) -> None:
        impact = _stage_impact(methodology, subject) if subject in methodology.condition_names() else 0.6
        cands = (InterventionKind.NO_ACTION,) + tuple(i for i in interventions if i is not InterventionKind.NO_ACTION)
        out.append(Blocker(
            prov=Provenance(provider=provider, evidence_refs=support),
            deal_ref=deal_ref, blocker_type=btype, subject=subject, detail=detail,
            confidence=round(confidence, 4), impact=impact, urgency=round(urgency, 4),
            resolvability=round(resolvability, 4), supporting_evidence=support,
            contradicting_evidence=against, candidate_interventions=cands))

    # evidence-quality blockers first (they gate the substantive reads)
    for name in readiness.conflicts:
        emit(BlockerType.EVIDENCE_CONFLICT, name, f"sources disagree on {name}", confidence=0.9,
             support=(f"{name}=CONFLICTED",), against=(), interventions=(_K.RESOLVE_CONFLICT, _K.VERIFY_CLAIM),
             resolvability=0.7, urgency=0.6)
    for name in readiness.stale:
        emit(BlockerType.STALE_EVIDENCE, name, f"{name} was satisfied but its evidence is past freshness",
             confidence=0.75, support=(f"{name}=STALE",), against=(), interventions=(_K.VERIFY_CLAIM,),
             resolvability=0.9, urgency=0.5)
    for name in readiness.inconsistencies:
        emit(BlockerType.PROCESS_INCONSISTENCY, name,
             f"{name} is reported satisfied but a prerequisite is not — likely reported out of order",
             confidence=0.7, support=(f"{name}=SATISFIED but dependency unmet",), against=(),
             interventions=(_K.VERIFY_CLAIM,), resolvability=0.6, urgency=0.4)

    # substantive blockers from the blocking conditions of the first incomplete stage
    conflicted = set(readiness.conflicts)
    stale = set(readiness.stale)
    for name in readiness.blocking_conditions:
        if name in conflicted or name in stale:
            continue                              # already covered by an evidence-quality blocker
        state = states.get(name, ConditionState.UNKNOWN)
        btype, interventions = _CONDITION_BLOCKER.get(name, _DEFAULT_BLOCKER)
        if state is ConditionState.UNKNOWN:
            emit(BlockerType.DISCOVERY_GAP, name, f"{name} has never been assessed — unknown, not disproven",
                 confidence=0.5, support=(f"{name}=UNKNOWN",), against=(),
                 interventions=(_K.VERIFY_CLAIM,) + interventions, resolvability=0.8, urgency=0.5)
        else:  # UNSATISFIED / PARTIAL
            conf = 0.8 if state is ConditionState.UNSATISFIED else 0.55
            emit(btype, name, f"{name} is {state.value}", confidence=conf,
                 support=(f"{name}={state.value}",), against=(), interventions=interventions,
                 resolvability=0.6, urgency=0.6)
    return tuple(out)


__all__ = ["BlockerType", "InterventionKind", "Blocker", "infer_blockers"]
