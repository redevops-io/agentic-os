"""Links communications to economic events, with the method named and a confidence that is a count of independent signals, not a model's feeling.

Order of evidence, strongest first:

1. ``explicit_identifier``: the text names a document (``ACC-SINV-2026-00009``). Certain that the message is about that document; it says nothing about WHY.
2. ``customer+time+amount``: same resolved customer, the message falls in the window before or after the event, and the text states a percentage or an amount that the event's own
   numbers reproduce (a 40 % discount on a 2,500 list price, a 420 shortfall). Two or three independent signals.
3. ``customer+time``: same customer and window, nothing else agrees. A candidate only: a complaint near a discount is not its reason.

A message whose customer could not be resolved by an explicit identifier is never linked by a name. ``status`` is ``linked`` (explicit identifier, or customer+time+amount where
exactly one event matches), ``candidate`` (anything weaker, or ambiguous between events) and ``ambiguous`` (two or more events fit equally well): a person or a later step decides.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contracts import EconomicEvent, Kind
from .evidence import Communication

DOC = re.compile(r"\b[A-Z]{2,5}-[A-Z]{2,5}-\d{4}-\d{3,6}(?:-\d+)?\b")
PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)|\b(\d{1,3}(?:,\d{3})+|\d{3,6})(?:\.\d{1,2})?\b")
WINDOW_BEFORE_DAYS, WINDOW_AFTER_DAYS = 45, 45
KIND_WORDS = {Kind.CANCELLATION: re.compile(r"\bcancel(?:l?ed|l?ing|lation)?\b", re.I),
              Kind.RETURN: re.compile(r"\b(?:refund(?:ed)?|return(?:ed)?|credit(?: note)?|replacement)\b", re.I),
              Kind.DISCOUNT: re.compile(r"\b(?:discount|goodwill|off|rebate|concession)\b|%", re.I)}


def _day(iso: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(iso[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class Mentions:
    documents: Tuple[str, ...]
    percents: Tuple[Decimal, ...]
    amounts: Tuple[Decimal, ...]


def mentions(text: str) -> Mentions:
    # a document name's digits are not amounts
    cleaned = DOC.sub(" ", text)
    amounts = []
    for m in MONEY.finditer(cleaned):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        if raw:
            amounts.append(Decimal(raw))
    return Mentions(tuple(sorted(set(DOC.findall(text)))), tuple(sorted({Decimal(p) for p in PCT.findall(text)})), tuple(sorted(set(amounts))))


@dataclass(frozen=True)
class CorrelationEdge:
    communication: str                 # EvidenceRef.id
    target: str                        # an economic event id, or a document name
    relation: str                      # "about_document" | "may_explain"
    method: str
    signals: Tuple[str, ...]           # what agreed: "document", "customer", "time", "percent", "amount"
    status: str                        # linked | candidate | ambiguous
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"communication": self.communication, "target": self.target, "relation": self.relation, "method": self.method, "signals": list(self.signals),
                "status": self.status, "note": self.note}


def _invoice_of(e: EconomicEvent) -> str:
    return e.subjects.get("invoice") or e.id.split(":")[0]


def _event_numbers(e: EconomicEvent, list_by_invoice: Mapping[str, Decimal]) -> Tuple[Set[Decimal], Set[Decimal]]:
    """The percentages and amounts an event's own numbers can be restated as."""
    amt = abs(e.amount)
    pcts: Set[Decimal] = set()
    lst = list_by_invoice.get(_invoice_of(e))
    if e.kind == Kind.DISCOUNT and lst:
        pcts.add((amt / lst * 100).quantize(Decimal("0.1")).normalize())
    return pcts, {amt.quantize(Decimal("1"))} | {amt.quantize(Decimal("0.01"))}


def correlate(comms: Sequence[Communication], events: Sequence[EconomicEvent], *, extra_amounts: Optional[Mapping[str, Sequence[Decimal]]] = None) -> List[CorrelationEdge]:
    """``extra_amounts`` maps an event id (or any target) to further amounts it can be restated as, for example a payment discrepancy's two sides."""
    edges: List[CorrelationEdge] = []
    list_by_invoice = {_invoice_of(e): e.amount for e in events if e.kind in (Kind.SALE, Kind.RETURN) and e.amount}
    by_invoice: Dict[str, List[EconomicEvent]] = {}
    for e in events:
        by_invoice.setdefault(_invoice_of(e), []).append(e)
    interesting = [e for e in events if e.kind in (Kind.DISCOUNT, Kind.RETURN, Kind.CANCELLATION)]
    for c in comms:
        mt = mentions(c.text)
        cid = c.ref.id
        for doc in mt.documents:                                          # 1. explicit identifiers
            hits = by_invoice.get(doc, [])
            edges.append(CorrelationEdge(cid, doc, "about_document", "explicit_identifier", ("document",), "linked",
                                         "" if hits else "the document is named but no economic event was read for it"))
        if not c.customer:
            continue                                                      # no explicit customer identity: never linked by a name
        day = _day(c.ref.at)
        scored: List[Tuple[int, EconomicEvent, Tuple[str, ...]]] = []
        for e in interesting:
            if e.subjects.get("customer") != c.customer:
                continue
            ed = _day(e.at)
            if not day or not ed or not (-WINDOW_BEFORE_DAYS <= (ed - day).days <= WINDOW_AFTER_DAYS):
                continue
            pcts, amts = _event_numbers(e, list_by_invoice)
            sig = ["customer", "time"]
            if any(p in {x.normalize() for x in mt.percents} for p in pcts):
                sig.append("percent")
            if any(a in set(mt.amounts) for a in amts) or any(Decimal(a) in set(mt.amounts) for a in (extra_amounts or {}).get(e.id, ())):
                sig.append("amount")
            if KIND_WORDS[e.kind if e.kind in KIND_WORDS else Kind.DISCOUNT].search(c.text):
                sig.append("kind")
            scored.append((len(sig), e, tuple(sig)))
        if not scored:
            continue
        top = max(s for s, _, _ in scored)
        best = [(e, sig) for s, e, sig in scored if s == top]
        for e, sig in best:
            states_figure = bool(mt.percents or mt.amounts)
            matched_figure = "percent" in sig or "amount" in sig
            # three signals are strong only if the text does not state a figure the event contradicts ("25% off" beside a 40% discount is not that discount)
            strong = top >= 3 and (matched_figure or ("kind" in sig and not states_figure))
            status = "ambiguous" if len(best) > 1 else ("linked" if strong else "candidate")
            method = "customer+time+" + "+".join(x for x in sig if x not in ("customer", "time")) if strong else "customer+time"
            edges.append(CorrelationEdge(cid, e.id, "may_explain", method, sig, status,
                                         "two or more events fit equally well" if len(best) > 1 else ("" if strong else "same customer and period only: not a reason")))
    return edges


def correlate_discrepancies(comms: Sequence[Communication], discrepancies: Sequence[Any]) -> List[CorrelationEdge]:
    """Links communications to a payment discrepancy (``reconcile.Discrepancy``): by a named invoice, or by the same customer stating one of its amounts."""
    edges: List[CorrelationEdge] = []
    for d in discrepancies:
        sides = {d.posted.quantize(Decimal("1")), d.bank.quantize(Decimal("1")), d.difference.quantize(Decimal("1"))}
        for c in comms:
            mt = mentions(c.text)
            sig: List[str] = []
            if any(i in mt.documents for i in d.invoices):
                sig.append("document")
            if c.customer and c.customer == d.customer:
                sig.append("customer")
                if sides & set(mt.amounts):
                    sig.append("amount")
            if "document" in sig or ("customer" in sig and "amount" in sig):
                edges.append(CorrelationEdge(c.ref.id, f"discrepancy:{d.reference}", "may_explain", "+".join(sig), tuple(sig),
                                             "linked" if len(sig) >= 2 else "candidate", "a customer's statement, not independent bank evidence"))
    return edges
