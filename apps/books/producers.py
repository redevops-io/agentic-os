"""Decision producer for books (runtime-native, plan §3.4/§5).

Bookkeeping state -> candidates over books' real capabilities. Posting the period close is the
approval-gated write (CONSEQUENTIAL, matching the operator); categorize / record_revenue are bounded
writes; reconcile proposes a reconciliation (read-tier).
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("books.categorize", "books.reconcile", "books.record_revenue", "books.close")


@dataclass(frozen=True)
class BooksSignals:
    entity: str = "the business"
    uncategorized: int = 0
    unreconciled: int = 0
    revenue_to_record: float = 0.0
    period_end: bool = False


def _c(entity, kind, action, cap, ev, tier, *, conf=0.75, urgency=0.3):
    return InterventionCandidate(
        source_app="books", subject=entity, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.5,
        required_capabilities=(cap,), candidate_id=f"books:{entity}:{kind}")


def books_opportunity(s: BooksSignals) -> DecisionOpportunity:
    a = []
    if s.uncategorized > 0:
        a.append(_c(s.entity, "categorize", "Categorise the uncategorised transactions",
                    "books.categorize", 0.4, RiskTier.BOUNDED_WRITE, urgency=0.3))
    if s.unreconciled > 0:
        a.append(_c(s.entity, "reconcile", "Propose reconciliations for open items",
                    "books.reconcile", 0.35, RiskTier.READ, urgency=0.3))
    if s.revenue_to_record > 0:
        a.append(_c(s.entity, "record_revenue", "Record the recognised revenue entry",
                    "books.record_revenue", 0.5, RiskTier.BOUNDED_WRITE, urgency=0.4))
    if s.period_end:
        a.append(_c(s.entity, "close", "Post the period-closing voucher", "books.close",
                    0.7, RiskTier.CONSEQUENTIAL, conf=0.8, urgency=0.6))
    return DecisionOpportunity(
        entity=s.entity, source_app="books", candidate_actions=tuple(a),
        evidence=(f"uncategorized={s.uncategorized}", f"unreconciled={s.unreconciled}",
                  f"period_end={s.period_end}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"books:{s.entity}")


__all__ = ["BooksSignals", "EMITTED_CAPABILITIES", "books_opportunity"]
