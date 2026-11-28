"""The five questions, answered from :class:`EconomicEvent` lists. Pure functions: events in, rows out, every row carrying its source records.

Every money figure is split into ``posted`` (written on a submitted accounting document) and ``estimated`` (derived), and the two are
never added into one unlabeled number except in a field that is named ``total`` beside both parts.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Sequence

from .contracts import Basis, EconomicEvent, Kind, in_period

ZERO = Decimal("0.00")
CUSTOMER_KINDS = {Kind.SALE: "list_revenue", Kind.RETURN: "list_revenue", Kind.DISCOUNT: "discounts", Kind.COST_OF_GOODS: "cost_of_goods",
                  Kind.SUPPORT_COST: "support", Kind.REWORK_COST: "rework", Kind.CANCELLATION_HANDLING: "cancellation_handling",
                  Kind.CANCELLATION: "cancellation_revenue_forgone"}


def _s(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def _window(events: Iterable[EconomicEvent], since: str, until: str) -> List[EconomicEvent]:
    return [e for e in events if in_period(e.at, since, until)]


def customer_profitability(events: Sequence[EconomicEvent], *, since: str = "", until: str = "") -> List[Dict[str, Any]]:
    """Q1. Per customer: revenue at list, discounts, net revenue, cost of goods, support, rework, cancellation handling, profit.
    A forgone-revenue estimate of a cancelled invoice is shown but not subtracted: that money was never earned, so it is not a loss of profit."""
    rows: Dict[str, Dict[str, Any]] = {}
    for e in _window(events, since, until):
        c = e.subjects.get("customer")
        if not c or e.kind not in CUSTOMER_KINDS:
            continue
        r = rows.setdefault(c, {"customer": c, "customer_source": e.subjects.get("customer_source", c), "currency": e.currency,
                                **{v: ZERO for v in set(CUSTOMER_KINDS.values())}, "posted": ZERO, "estimated": ZERO, "sources": []})
        r[CUSTOMER_KINDS[e.kind]] += e.amount
        if e.kind != Kind.CANCELLATION:
            r["posted" if e.basis == Basis.POSTED else "estimated"] += e.amount
        r["sources"].extend(s for s in e.sources if s not in r["sources"])
    out = []
    for r in rows.values():
        net = r["list_revenue"] + r["discounts"]
        profit = r["posted"] + r["estimated"]
        out.append({**{k: (_s(v) if isinstance(v, Decimal) else v) for k, v in r.items()}, "net_revenue": _s(net), "profit": _s(profit),
                    "profit_posted_part": _s(r["posted"]), "profit_estimated_part": _s(r["estimated"]),
                    "margin_pct": (_s((profit / net * 100).quantize(Decimal("0.1"))) if net > 0 else None), "unprofitable": profit < 0})
    return sorted(out, key=lambda x: Decimal(x["profit"]))


def cancellation_cost(events: Sequence[EconomicEvent], *, since: str, until: str) -> Dict[str, Any]:
    """Q2. What cancellations cost in the period. The posted effect of a cancelled invoice is nil (its posting was reversed); what it cost is the
    handling time logged against it, and the revenue it forwent when nothing replaced it. Both are estimates and are reported as such."""
    ev = _window(events, since, until)
    cancelled = [e for e in ev if e.kind == Kind.CANCELLATION]
    handling = [e for e in ev if e.kind == Kind.CANCELLATION_HANDLING]
    forgone = -sum((e.amount for e in cancelled), ZERO)
    cost = -sum((e.amount for e in handling), ZERO)
    return {"since": since, "until": until, "invoices_cancelled": len(cancelled), "replaced": sum(1 for e in cancelled if e.note.startswith("replaced")),
            "revenue_forgone_estimated": _s(forgone), "handling_cost_estimated": _s(cost), "posted_effect": _s(ZERO),
            "total_estimated": _s(forgone + cost),
            "invoices": [{"invoice": e.subjects.get("invoice"), "customer": e.subjects.get("customer"), "note": e.note, "forgone": _s(-e.amount),
                          "sources": list(e.sources)} for e in cancelled],
            "handling": [{"invoice": e.subjects.get("invoice"), "customer": e.subjects.get("customer"), "cost": _s(-e.amount), "sources": list(e.sources)}
                         for e in handling]}


def discount_loss(events: Sequence[EconomicEvent], *, since: str = "", until: str = "") -> Dict[str, Any]:
    """Q3. What discounts gave up (list price minus what was invoiced) and who the document CLAIMS approved each. The claim is a comment anyone with
    access to the application could write, so it is reported as an unverified claim, never as an approval. A discount with no claim is reported as such."""
    ev = [e for e in _window(events, since, until) if e.kind == Kind.DISCOUNT]
    total = -sum((e.amount for e in ev), ZERO)
    by: Dict[str, Decimal] = {}
    none = ZERO
    for e in ev:
        if e.approval:
            by[e.approval.approver] = by.get(e.approval.approver, ZERO) - e.amount
        else:
            none -= e.amount
    return {"since": since, "until": until, "total_posted": _s(total), "with_approval_claim": _s(total - none), "without_approval_claim": _s(none),
            "approval_evidence": "unverified: read from a comment on the document, which is not proof that anyone approved",
            "by_claimed_approver": {k: _s(v) for k, v in sorted(by.items())},
            "discounts": [{"invoice": e.subjects.get("invoice"), "customer": e.subjects.get("customer"), "amount": _s(-e.amount),
                           "claimed_approval": (e.approval.id if e.approval else None), "claimed_approver": (e.approval.approver if e.approval else None),
                           "sources": list(e.sources)} for e in sorted(ev, key=lambda x: x.amount)]}


def supplier_cost(events: Sequence[EconomicEvent], *, since: str = "", until: str = "") -> List[Dict[str, Any]]:
    """Q4. Per supplier: net purchases (posted, after debit notes) plus the estimated cost of rework the time logs attribute to that supplier."""
    rows: Dict[str, Dict[str, Any]] = {}
    for e in _window(events, since, until):
        s = e.subjects.get("supplier")
        if not s or e.kind not in (Kind.SUPPLIER_COST, Kind.SUPPLIER_RETURN, Kind.REWORK_COST):
            continue
        r = rows.setdefault(s, {"supplier": s, "net_purchases_posted": ZERO, "rework_estimated": ZERO, "sources": []})
        r["net_purchases_posted" if e.kind != Kind.REWORK_COST else "rework_estimated"] += -e.amount
        r["sources"].extend(x for x in e.sources if x not in r["sources"])
    out = [{**{k: (_s(v) if isinstance(v, Decimal) else v) for k, v in r.items()},
            "total": _s(r["net_purchases_posted"] + r["rework_estimated"])} for r in rows.values()]
    return sorted(out, key=lambda x: -Decimal(x["total"]))
