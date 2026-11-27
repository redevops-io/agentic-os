"""Reconciliation in both directions, from what the other modules established. Nothing here reads an application or calls a model: it takes events, explanations and matches
and says which of five things is true, with the evidence ids that show it.

* ``recorded_without_rationale``: an adjustment (discount or credit note) is in the books and no communication states why (the explanation class is ``unknown``). "No reason found"
  is not "no reason exists": the note says which sources were read.
* ``commitment_without_record``: the company confirmed a monetary commitment to a customer in a customer-facing conversation and no native record carries it.
* ``decision_not_executed``: the same, where every statement of it is internal (a private note or a CRM note): a decision somebody wrote down that never reached the books.
* ``contradicts_record``: a message states a percentage the recorded discount does not reproduce, while nothing else the customer was told in that period does; or a commitment and
  its record differ in amount or currency (never converted).
* ``adjustment_without_verified_authorization``: a discount or credit note with no approval that the trusted side has verified. A comment saying "approved" is a claim, shown as one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .contracts import EconomicEvent, Kind
from .correlate import WINDOW_AFTER_DAYS, WINDOW_BEFORE_DAYS, _day, _event_numbers, _invoice_of, mentions
from .evidence import Communication

KINDS = ("recorded_without_rationale", "commitment_without_record", "decision_not_executed", "contradicts_record", "adjustment_without_verified_authorization")
INTERNAL = {"internal_note", "crm_note"}
ADJUSTMENTS = (Kind.DISCOUNT, Kind.RETURN)


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str                       # an economic event id, or a commitment id
    customer: Optional[str]
    amount: Optional[Decimal]
    currency: str
    evidence: Sequence[str] = ()       # communication ids that show it
    sources: Sequence[str] = ()        # the records it concerns
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "customer": self.customer, "amount": str(self.amount) if self.amount is not None else None, "currency": self.currency,
                "evidence": list(self.evidence), "sources": list(self.sources), "note": self.note}


def _contradictions(events: Sequence[EconomicEvent], comms: Sequence[Communication]) -> List[Finding]:
    list_by_invoice = {_invoice_of(e): e.amount for e in events if e.kind in (Kind.SALE, Kind.RETURN) and e.amount}
    out: List[Finding] = []
    for e in events:
        if e.kind != Kind.DISCOUNT:
            continue
        pcts, _ = _event_numbers(e, list_by_invoice)
        ed = _day(e.at)
        if not pcts or not ed:
            continue
        near = []
        for c in comms:
            d = _day(c.ref.at)
            if c.customer == e.subjects.get("customer") and d and -WINDOW_BEFORE_DAYS <= (ed - d).days <= WINDOW_AFTER_DAYS and mentions(c.text).percents:
                near.append(c)
        stated = {p.normalize() for c in near for p in mentions(c.text).percents}
        if near and not (stated & {p.normalize() for p in pcts}):
            out.append(Finding("contradicts_record", e.id, e.subjects.get("customer"), abs(e.amount), e.currency, [c.ref.id for c in near], e.sources,
                               f"messages to this customer in the period state {sorted(str(s) for s in stated)} percent; the recorded discount is {sorted(str(p) for p in pcts)} percent"))
    return out


def reconcile_both(events: Sequence[EconomicEvent], explanations: Mapping[str, Any], commitments: Sequence[Any], matches: Sequence[Any], comms: Sequence[Communication]) -> List[Finding]:
    """``explanations`` maps an event id to ``reasoning.Explanation``; an adjustment with no entry was not examined and is not reported as lacking a rationale."""
    by_id = {c.ref.id: c for c in comms}
    out: List[Finding] = []
    for e in events:
        if e.kind not in ADJUSTMENTS:
            continue
        cust = e.subjects.get("customer")
        x = explanations.get(e.id)
        if x is not None and x.cls == "unknown":
            out.append(Finding("recorded_without_rationale", e.id, cust, abs(e.amount), e.currency, x.evidence, e.sources, "; ".join(x.notes) or "no communication read states a reason"))
        if not (e.approval and e.approval.verified):
            claim = f"an unverified claim in a comment ({e.approval.approver}, {e.approval.id})" if e.approval else "no claim of approval was found either"
            out.append(Finding("adjustment_without_verified_authorization", e.id, cust, abs(e.amount), e.currency, (), e.sources, claim))
    cmap = {c.id: c for c in commitments}
    for m in matches:
        c = cmap.get(m.commitment)
        if c is None:
            continue
        roles = {by_id[i].ref.role for i in c.evidence if i in by_id}
        if m.status == "missing":
            kind = "decision_not_executed" if roles and roles <= INTERNAL else "commitment_without_record"
            out.append(Finding(kind, c.id, c.customer, c.amount, c.currency, c.evidence, (), m.note))
        elif m.status == "inconsistent":
            out.append(Finding("contradicts_record", c.id, c.customer, c.amount, c.currency, c.evidence, m.records, m.note))
    out += _contradictions(events, comms)
    return out
