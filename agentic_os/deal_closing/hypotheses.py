"""Closing hypotheses (Phase 3, plan §11).

A :class:`Blocker` says *what is probably wrong*. A :class:`ClosingHypothesis` is the reasoned, falsifiable step
from that cause to a proposed response: observation → inferred cause → mechanism → expected consequence →
confidence → **contradicting evidence** → candidate interventions → a verification step. The engine never jumps
from a missing condition straight to an action; a hypothesis must survive verification (supporting outweighs
contradicting, above a confidence bar) before it is ``actionable``. This mirrors the discovery plane's
``DeterministicHypothesisPlanner`` whose verification plan ends in "confirm_or_abstain: promote only if supporting
outweighs contradicting".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Mapping, Tuple

from ..integrations.business.contracts import BusinessObject, Provenance
from .blockers import Blocker, BlockerType, InterventionKind

# Per-blocker mechanism + consequence phrasing (the deterministic "why this stalls the deal" narrative).
_NARRATIVE: Mapping[BlockerType, Tuple[str, str]] = {
    BlockerType.DISCOVERY_GAP: ("the problem/value is not established, so the buyer has no reason to prioritize",
                                "the deal slips as other priorities win the buyer's attention"),
    BlockerType.METRICS_UNQUANTIFIED: ("without quantified impact the buyer can't build an internal case",
                                       "no budget is allocated and the deal stalls at justification"),
    BlockerType.STAKEHOLDER_GAP: ("a required decision-maker is not identified, so decisions can't be made",
                                  "the deal waits indefinitely on an absent approver"),
    BlockerType.STAKEHOLDER_CONFLICT: ("two people claim the same deciding role, so authority is ambiguous",
                                       "conflicting direction stalls or reverses the decision"),
    BlockerType.CHAMPION_WEAK: ("the champion lacks influence to drive the process internally",
                                "momentum decays between meetings"),
    BlockerType.NO_ECONOMIC_BUYER_ACCESS: ("the economic buyer is not engaged, so spend can't be authorized",
                                           "the deal cannot be signed regardless of technical fit"),
    BlockerType.DECISION_PROCESS_UNKNOWN: ("the buying/approval process is unmapped, so steps are missed",
                                           "surprises (procurement, security) appear late and delay close"),
    BlockerType.DECISION_CRITERIA_UNKNOWN: ("the evaluation criteria are unknown, so value isn't aimed",
                                            "a competitor better aligned to the criteria wins"),
    BlockerType.COMPETITIVE_THREAT: ("a competitor is better positioned on the known criteria",
                                     "the deal is lost or heavily discounted"),
    BlockerType.TECHNICAL_BLOCKER: ("technical validation is incomplete, leaving unaddressed risk",
                                    "the buyer withholds commitment pending proof"),
    BlockerType.SECURITY_BLOCKER: ("security review is not complete, a hard gate for most buyers",
                                   "the close date is unattainable until it clears"),
    BlockerType.LEGAL_BLOCKER: ("legal/contract review is outstanding",
                                "redlines and signature slip past the target date"),
    BlockerType.PROCUREMENT_BLOCKER: ("procurement is not engaged, and it has its own timeline",
                                      "a late procurement cycle pushes the close out weeks"),
    BlockerType.PRICING_GAP: ("pricing is not agreed, so there is nothing to sign",
                              "negotiation drags and discount pressure grows"),
    BlockerType.QUOTE_STALLED: ("the quote is undelivered or unanswered",
                                "the buyer's urgency fades and the quote goes cold"),
    BlockerType.BUDGET_UNCONFIRMED: ("budget is not confirmed, so funding is uncertain",
                                     "the deal is deferred to a future budget cycle"),
    BlockerType.PAPER_PROCESS_UNKNOWN: ("the paperwork/signature path is unknown",
                                        "the final mile takes far longer than expected"),
    BlockerType.NO_MUTUAL_PLAN: ("there is no agreed close plan with dated steps",
                                 "both sides drift without a shared critical path"),
    BlockerType.CONTRACT_STALLED: ("the contract is not progressing toward signature",
                                   "the close date passes unmet"),
    BlockerType.EVIDENCE_CONFLICT: ("the systems of record disagree, so the true state is unknown",
                                    "decisions built on the wrong value are wasted or harmful"),
    BlockerType.STALE_EVIDENCE: ("the confirming evidence is old and may no longer hold",
                                 "a condition believed met may have silently regressed"),
    BlockerType.PROCESS_INCONSISTENCY: ("a later step is marked done while a prerequisite is not",
                                        "the pipeline reflects an order that did not actually happen"),
    BlockerType.UNKNOWN: ("the cause is not yet classified", "the deal is stuck for an undetermined reason"),
}


@dataclass(frozen=True)
class ClosingHypothesis(BusinessObject):
    """A falsifiable explanation + proposed response for one blocker. ``candidate_interventions`` are options to
    be scored in Phase 4, not a committed plan; ``verification`` is what to confirm before acting."""
    KIND: ClassVar[str] = "deal_closing.hypothesis"
    deal_ref: str = ""
    blocker_type: BlockerType = BlockerType.UNKNOWN
    subject: str = ""
    observation: str = ""
    inferred_cause: str = ""
    mechanism: str = ""
    expected_consequence: str = ""
    confidence: float = 0.0
    impact: float = 0.0                           # carried from the blocker (0..1, threat to the close)
    urgency: float = 0.0                          # carried from the blocker (0..1, time-sensitivity)
    supporting_evidence: Tuple[str, ...] = ()
    contradicting_evidence: Tuple[str, ...] = ()
    candidate_interventions: Tuple[InterventionKind, ...] = ()
    verification: str = ""

    def is_actionable(self, *, bar: float = 0.5) -> bool:
        """A hypothesis is actionable only when supporting evidence outweighs contradicting AND confidence clears
        the bar. Otherwise it needs verification first (the honest default)."""
        if len(self.contradicting_evidence) >= len(self.supporting_evidence):
            return False
        return self.confidence >= bar

    @property
    def actionable(self) -> bool:
        return self.is_actionable()


def hypothesis_for(blocker: Blocker, *, provider: str = "deal_closing") -> ClosingHypothesis:
    """Build the hypothesis for a single blocker. Confidence is the blocker's, discounted when contradicting
    evidence is present; verification re-reads the system of record / stakeholder before any action."""
    mechanism, consequence = _NARRATIVE.get(blocker.blocker_type, _NARRATIVE[BlockerType.UNKNOWN])
    contra = blocker.contradicting_evidence
    confidence = blocker.confidence * (0.6 if contra else 1.0)
    verification = (f"re-read the system(s) of record for {blocker.subject} and confirm with the relevant "
                    f"stakeholder before acting") if blocker.subject else "confirm the cause before acting"
    return ClosingHypothesis(
        prov=Provenance(provider=provider, evidence_refs=blocker.supporting_evidence),
        deal_ref=blocker.deal_ref, blocker_type=blocker.blocker_type, subject=blocker.subject,
        observation=blocker.detail or f"{blocker.subject} is blocking",
        inferred_cause=blocker.blocker_type.value, mechanism=mechanism, expected_consequence=consequence,
        confidence=round(confidence, 4), impact=blocker.impact, urgency=blocker.urgency,
        supporting_evidence=blocker.supporting_evidence,
        contradicting_evidence=contra, candidate_interventions=blocker.candidate_interventions,
        verification=verification)


def plan_hypotheses(blockers: Tuple[Blocker, ...], *, provider: str = "deal_closing") -> Tuple[ClosingHypothesis, ...]:
    """One hypothesis per blocker, ordered by expected damage (impact × confidence) so the most threatening,
    best-supported explanations lead. Ranking is NOT a decision — Phase 4 scores the interventions."""
    hyps = [hypothesis_for(b, provider=provider) for b in blockers]
    order = {id(h): (b.impact * b.confidence) for h, b in zip(hyps, blockers)}
    return tuple(sorted(hyps, key=lambda h: order[id(h)], reverse=True))


__all__ = ["ClosingHypothesis", "hypothesis_for", "plan_hypotheses"]
