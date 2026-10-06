"""Candidate actions + scoring (Phase 4, plan §13).

A hypothesis offers candidate intervention KINDS; this module turns each into a concrete, scorable
:class:`CandidateAction` (what it targets, who performs it, its effect, cost, value, reversibility, the authority
it needs, and the condition to re-read to CONFIRM it worked) and ranks them by reusing the existing optimiser
(:mod:`agentic_os.priority_engine`) rather than writing a second scorer. ``NO_ACTION`` is always a candidate and
the baseline every real action must beat — the thing that stops the system acting by default.

The score is deliberately NOT a fabricated close-probability: via the adapter it becomes
``confidence × expected_value`` where confidence = P(the blocker is real) and expected_value = deal-value-at-stake
× P(the action resolves the blocker), then urgency-boosted and risk/cost-penalised by the engine (§18).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..priority_engine import InterventionCandidate, InterventionDecision, PriorityPolicy, decide, do_nothing
from .autonomy import risk_tier_for
from .blockers import InterventionKind
from .hypotheses import ClosingHypothesis

# Per-kind defaults: (P(resolves the blocker), execution_cost 0..1, reversibility 0..1, actor, effect phrase).
_K = InterventionKind
_KIND_DEFAULTS = {
    _K.NO_ACTION: (0.0, 0.0, 1.0, "system", "take no action"),
    _K.VERIFY_CLAIM: (0.6, 0.05, 1.0, "system", "re-read the system of record"),
    _K.RESOLVE_CONFLICT: (0.55, 0.2, 1.0, "rep", "reconcile the disagreeing sources"),
    _K.QUALIFY_DISCOVERY: (0.5, 0.2, 1.0, "rep", "run a discovery conversation"),
    _K.IDENTIFY_STAKEHOLDER: (0.55, 0.15, 1.0, "rep", "identify the missing stakeholder"),
    _K.ENGAGE_ECONOMIC_BUYER: (0.45, 0.3, 0.9, "rep", "get a meeting with the economic buyer"),
    _K.STRENGTHEN_CHAMPION: (0.4, 0.25, 1.0, "rep", "equip the champion to sell internally"),
    _K.MAP_DECISION_PROCESS: (0.6, 0.2, 1.0, "rep", "map the decision and paper process"),
    _K.CLARIFY_CRITERIA: (0.6, 0.15, 1.0, "rep", "confirm the decision criteria"),
    _K.COMPETITIVE_DIFFERENTIATION: (0.45, 0.25, 0.9, "rep", "differentiate against the competitor"),
    _K.REQUEST_TECHNICAL_VALIDATION: (0.55, 0.3, 0.9, "rep", "run the technical validation"),
    _K.REQUEST_SECURITY_REVIEW: (0.6, 0.3, 0.9, "rep", "initiate the security review"),
    _K.ADVANCE_LEGAL_REVIEW: (0.5, 0.4, 0.7, "rep", "advance the legal/contract review"),
    _K.ENGAGE_PROCUREMENT: (0.55, 0.3, 0.9, "rep", "engage procurement early"),
    _K.AGREE_PRICING: (0.5, 0.3, 0.6, "rep", "agree pricing"),
    _K.SEND_QUOTE: (0.6, 0.2, 0.8, "rep", "deliver the quote"),
    _K.FOLLOW_UP_QUOTE: (0.5, 0.1, 0.9, "rep", "follow up on the open quote"),
    _K.CONFIRM_BUDGET: (0.5, 0.2, 1.0, "rep", "confirm budget is allocated"),
    _K.PREPARE_MUTUAL_PLAN: (0.6, 0.2, 1.0, "rep", "agree a mutual close plan"),
    _K.SEND_CONTRACT: (0.6, 0.3, 0.7, "rep", "send the contract"),
    _K.ADVANCE_SIGNATURE: (0.55, 0.3, 0.7, "rep", "advance to signature"),
    _K.ESCALATE: (0.4, 0.2, 1.0, "manager", "escalate internally"),
}
_DEFAULT = (0.4, 0.3, 0.9, "rep", "intervene")


@dataclass(frozen=True)
class CandidateAction:
    """A concrete, scorable action proposed against one blocker. ``verification_condition`` is the condition the
    runtime re-reads AFTER executing to confirm the external world actually changed (closing the loop)."""
    kind: InterventionKind
    deal_ref: str = ""
    target_condition: str = ""
    actor: str = "rep"
    effect: str = ""
    p_blocker_real: float = 0.0           # P(the blocker is real) → ranking confidence
    p_resolves: float = 0.0               # P(this action resolves the blocker)
    value_cents: int = 0                  # deal value at stake this could unlock
    urgency: float = 0.0
    execution_cost: float = 0.0
    reversibility: float = 1.0
    required_authority: object = None     # a RiskTier (None → derived from kind)
    deadline_ms: int = 0
    verification_condition: str = ""
    evidence_refs: Tuple[str, ...] = ()

    def authority(self):
        return self.required_authority if self.required_authority is not None else risk_tier_for(self.kind)


def no_action(deal_ref: str = "", target_condition: str = "") -> CandidateAction:
    return CandidateAction(kind=InterventionKind.NO_ACTION, deal_ref=deal_ref,
                           target_condition=target_condition, effect="take no action",
                           p_resolves=0.0, reversibility=1.0)


def candidates_from_hypothesis(hyp: ClosingHypothesis, *, deal_value_cents: int = 0) -> Tuple[CandidateAction, ...]:
    """Expand a hypothesis's candidate intervention kinds into concrete, valued actions. Value at stake scales
    with the blocker's impact so a late-stage blocker's actions are worth more. A non-actionable hypothesis
    (contradicted / low confidence) yields ONLY verification + no-action — we verify before we act."""
    value = int(deal_value_cents * max(hyp.impact, 0.1))
    actionable = hyp.is_actionable()
    out: List[CandidateAction] = []
    for kind in hyp.candidate_interventions:
        if not actionable and kind not in (InterventionKind.NO_ACTION, InterventionKind.VERIFY_CLAIM,
                                           InterventionKind.RESOLVE_CONFLICT):
            continue
        if kind is InterventionKind.NO_ACTION:
            out.append(no_action(hyp.deal_ref, hyp.subject))
            continue
        p_res, cost, rev, actor, effect = _KIND_DEFAULTS.get(kind, _DEFAULT)
        out.append(CandidateAction(
            kind=kind, deal_ref=hyp.deal_ref, target_condition=hyp.subject, actor=actor, effect=effect,
            p_blocker_real=hyp.confidence, p_resolves=p_res, value_cents=value, urgency=hyp.urgency,
            execution_cost=cost, reversibility=rev, verification_condition=hyp.subject,
            evidence_refs=hyp.supporting_evidence))
    if not any(a.kind is InterventionKind.NO_ACTION for a in out):
        out.insert(0, no_action(hyp.deal_ref, hyp.subject))
    return tuple(out)


def to_priority_candidate(action: CandidateAction, *, value_scale_cents: int = 100_00) -> InterventionCandidate:
    """Adapt a CandidateAction onto the priority engine (mirrors product_opportunity.to_priority_candidate).
    ``expected_value`` = (value at stake / scale) × P(resolves); ``confidence`` = P(blocker real); the risk tier
    carries the required authority so the engine routes the approval gate. NO_ACTION maps to do_nothing()."""
    if action.kind is InterventionKind.NO_ACTION:
        return do_nothing(subject=action.target_condition or action.deal_ref, source_app="deal_closing")
    expected_value = round((action.value_cents / max(value_scale_cents, 1)) * action.p_resolves, 4)
    return InterventionCandidate(
        source_app="deal_closing", subject=f"{action.deal_ref}:{action.target_condition}",
        proposed_action=action.effect or action.kind.value, expected_value=expected_value,
        confidence=action.p_blocker_real, urgency=action.urgency, execution_cost=action.execution_cost,
        risk_tier=action.authority(), reversibility=action.reversibility, action_kind=action.kind.value,
        observation_refs=action.evidence_refs)


@dataclass(frozen=True)
class ScoredAction:
    """A candidate action with the engine's decision + explainable priority."""
    action: CandidateAction
    decision: InterventionDecision

    @property
    def total(self) -> float:
        return self.decision.priority.total


def score_actions(actions: Tuple[CandidateAction, ...], *, policy: Optional[PriorityPolicy] = None,
                  value_scale_cents: int = 100_00) -> List[ScoredAction]:
    """Score every candidate action via the priority engine and return them ranked best-first. NO_ACTION is kept
    so the caller can see what beating it looks like."""
    scored = [ScoredAction(a, decide(to_priority_candidate(a, value_scale_cents=value_scale_cents), policy))
              for a in actions]
    return sorted(scored, key=lambda s: s.total, reverse=True)


def best_action(actions: Tuple[CandidateAction, ...], *, policy: Optional[PriorityPolicy] = None,
                value_scale_cents: int = 100_00) -> ScoredAction:
    """The single best action — the top-ranked real action whose decision is not ABSTAIN AND whose full
    (cost-and-risk-adjusted) score beats doing nothing; otherwise NO_ACTION. This enforces 'do nothing unless
    something genuinely beats it' using the whole score, not just the value sign the engine abstains on."""
    from ..priority_engine import Action
    ranked = score_actions(actions, policy=policy, value_scale_cents=value_scale_cents)
    baseline = next((s for s in ranked if s.action.kind is InterventionKind.NO_ACTION), None)
    floor = baseline.total if baseline is not None else 0.0
    for s in ranked:
        if (s.action.kind is not InterventionKind.NO_ACTION
                and s.decision.action is not Action.ABSTAIN and s.total > floor):
            return s
    return baseline if baseline is not None else ranked[0]


__all__ = [
    "CandidateAction", "ScoredAction", "no_action", "candidates_from_hypothesis",
    "to_priority_candidate", "score_actions", "best_action",
]
