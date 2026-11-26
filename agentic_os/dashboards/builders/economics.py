"""What operations did to money: profit per customer after discounts, support and rework; what cancellations and discounts cost and who
approved the discounts; supplier cost; payments the bank does not confirm.

Built on the economic-event projection of ERPNext (``agentic_os.economics``), not on a second ledger. Every figure is split into the part written
on submitted accounting documents ("posted") and the part derived from hours or from revenue a cancellation forwent ("estimated"); the two are
never merged into one unlabeled number.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, List, Sequence

from agentic_os.economics import answers
from agentic_os.economics.contracts import Approval, Basis, EconomicEvent, Kind, money

from ..build import Build, col, panel, row
from ..contracts import Record
from ..store import day_of


def _event(r: Record) -> EconomicEvent:
    f = r.fields
    subj = {k: str(f[k]) for k in ("customer", "supplier", "invoice") if f.get(k)}
    subj["customer_source"] = subj.get("customer", "")
    return EconomicEvent(r.id, Kind(str(f.get("kind"))), Basis(str(f.get("basis"))), money(f.get("amount")), str(f.get("currency") or ""), day_of(r.at),
                         subj, (r.ref,) if r.ref else (), approval=(Approval(str(f["approval"]), str(f.get("approver") or "")) if f.get("approval") else None),
                         note=str(f.get("note") or ""))


def _docs(sources: Any, limit: int = 4) -> str:
    """The documents behind an aggregate row, by name (``ACC-SINV-2026-00001``), so a reader can open them; the row itself has no single document."""
    names = [str(x).split("/", 1)[-1] for x in sources]
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _handling_for(cc: Any, invoice: Any) -> str:
    total = sum((Decimal(h["cost"]) for h in cc["handling"] if h["invoice"] == invoice), Decimal("0"))
    return str(total.quantize(Decimal("0.01")))


def build(b: Build) -> None:
    d, S = b.d, ("economics",)
    if not b.usable("economics"):
        b.down("The accounting system cannot be read right now.", "profit", "Profit after discounts, support and rework", S)
        return
    recs = b.records("economics", "event")
    events = [_event(r) for r in recs]
    disc = b.records("economics", "discrepancy", timed=False)
    cur = next((e.currency for e in events if e.currency), "")
    cust = answers.customer_profitability(events)
    bad = [c for c in cust if c["unprofitable"]]
    dl = answers.discount_loss(events)
    sup = answers.supplier_cost(events)
    cc = answers.cancellation_cost(events, since="", until="")
    by_id = {r.id: r for r in recs}

    def drill_of(key: str, title: str, ids: Sequence[str]) -> str:
        return b.drill(key, title, [by_id[i] for i in ids if i in by_id], columns=("kind", "basis", "amount", "customer", "supplier", "invoice", "approver"))

    bad_names = {c["customer"] for c in bad}
    drill_of("customers:unprofitable", "Events of the unprofitable customers", [e.id for e in events if e.subjects.get("customer") in bad_names])
    drill_of("discounts", "Discounts given in range", [e.id for e in events if e.kind == Kind.DISCOUNT])
    drill_of("discounts:unclaimed", "Discounts with no approval claim", [e.id for e in events if e.kind == Kind.DISCOUNT and not e.approval])
    drill_of("cancellations", "Cancellations in range", [e.id for e in events if e.kind in (Kind.CANCELLATION, Kind.CANCELLATION_HANDLING)])
    drill_of("suppliers", "Supplier cost events", [e.id for e in events if e.subjects.get("supplier")])
    b.drill("payments:mismatch", "Payments the bank statement does not confirm", disc, columns=("posted", "bank", "difference", "customer", "invoices"))
    posted_net = sum((Decimal(c["net_revenue"]) for c in cust), Decimal("0"))
    d.panels.append(panel("summary", "Profit and loss events", "stats", sources=S, span=2, metrics=[
        b.metric("net_revenue", "Net revenue (posted)", needs=S, value=float(posted_net), unit=cur, drill="discounts", note="after discounts and credit notes, per submitted invoice"),
        b.metric("unprofitable", "Unprofitable customers", needs=S, value=len(bad), drill="customers:unprofitable", tone="bad" if bad else "ok", good="down",
                 note="profit after cost of goods, support, rework and cancellation handling; the last three are estimates"),
        b.metric("discounts", "Discounts given (posted)", needs=S, value=float(Decimal(dl["total_posted"])), unit=cur, drill="discounts"),
        b.metric("discounts_unref", "Discounts with no approval claim", needs=S, value=float(Decimal(dl["without_approval_claim"])), unit=cur,
                 drill="discounts:unclaimed", tone="warn" if Decimal(dl["without_approval_claim"]) else "ok", good="down",
                 note="no claim on the document; a claim, where there is one, is only a comment and is not proof of approval"),
        b.metric("cancel_cost", "Cancellation cost (estimated)", needs=S, value=float(Decimal(cc["total_estimated"])), unit=cur, drill="cancellations",
                 note="revenue forgone where nothing replaced the invoice, plus handling time; the posted effect is nil"),
        b.metric("mismatch", "Payments not matching the bank", needs=S, value=len(disc), drill="payments:mismatch" if disc else "", tone="bad" if disc else "ok", good="down")]))
    d.panels.append(panel("customers", "Profit by customer", "table", sources=S, span=2, empty="" if cust else "No sales in this range.",
                          rows=[row({"customer": c["customer"], "net": c["net_revenue"], "goods": c["cost_of_goods"], "support": c["support"],
                                     "rework": c["rework"], "handling": c["cancellation_handling"], "profit": c["profit"], "margin": c["margin_pct"] or "—", "documents": _docs(c["sources"])},
                                    tone="bad" if c["unprofitable"] else "") for c in cust],
                          columns=[col("customer", "Customer"), col("net", "Net revenue", "right"), col("goods", "Goods", "right"), col("support", "Support (est.)", "right"),
                                   col("rework", "Rework (est.)", "right"), col("handling", "Cancel handling (est.)", "right"), col("profit", "Profit", "right"),
                                   col("margin", "Margin %", "right"), col("documents", "Documents")]))
    d.panels.append(panel("discount_rows", "Discounts and who the document claims approved them (unverified)", "table", sources=S, span=2, empty="" if dl["discounts"] else "No discount in this range.",
                          rows=[row({"invoice": x["invoice"], "customer": x["customer"], "amount": x["amount"], "claim": x["claimed_approval"] or "no claim",
                                     "claimed_by": x["claimed_approver"] or "—"}, tone="" if x["claimed_approval"] else "warn", ref=(x["sources"] or [""])[0]) for x in dl["discounts"]],
                          columns=[col("invoice", "Invoice"), col("customer", "Customer"), col("amount", "Discount", "right"), col("claim", "Approval claim (unverified)"), col("claimed_by", "Claimed approver (unverified)")]))
    d.panels.append(panel("supplier_rows", "Supplier cost", "table", sources=S, empty="" if sup else "No supplier cost in this range.",
                          rows=[row({"supplier": x["supplier"], "purchases": x["net_purchases_posted"], "rework": x["rework_estimated"], "total": x["total"], "documents": _docs(x["sources"])})
                                for x in sup],
                          columns=[col("supplier", "Supplier"), col("purchases", "Net purchases (posted)", "right"), col("rework", "Rework (estimated)", "right"),
                                   col("total", "Total", "right"), col("documents", "Documents")]))
    d.panels.append(panel("cancellation_rows", "Cancellations and what they cost", "table", sources=S, span=2,
                          empty="" if cc["invoices"] else "No invoice was cancelled in this range.",
                          rows=[row({"invoice": x["invoice"], "customer": x["customer"], "outcome": x["note"], "forgone": x["forgone"], "handling": h},
                                    tone="warn" if not x["note"].startswith("replaced") else "") for x, h in
                                [(x, _handling_for(cc, x["invoice"])) for x in cc["invoices"]]],
                          columns=[col("invoice", "Cancelled invoice"), col("customer", "Customer"), col("outcome", "Outcome"),
                                   col("forgone", "Revenue forgone (estimated)", "right"), col("handling", "Handling time (estimated)", "right")]))
    d.panels.append(panel("approver_rows", "Discounts by claimed approver (unverified)", "table", sources=S, empty="" if dl["discounts"] else "No discount in this range.",
                          rows=[row({"approver": k, "amount": v}) for k, v in dl["by_claimed_approver"].items()] +
                               [row({"approver": "no approval claim", "amount": dl["without_approval_claim"]}, tone="warn")],
                          columns=[col("approver", "Claimed approver (unverified)"), col("amount", "Discounts claimed as approved", "right")]))
    d.panels.append(panel("mismatch_rows", "Payments the bank does not confirm", "table", sources=S, empty="" if disc else "Every payment with a bank line matches it.",
                          rows=[row({"reference": r.id, "customer": r.get("customer"), "posted": r.get("posted"), "bank": r.get("bank"), "difference": r.get("difference"),
                                     "invoices": r.get("invoices")}, tone="bad", ref=r.ref) for r in disc],
                          columns=[col("reference", "Reference"), col("customer", "Customer"), col("posted", "Books", "right"), col("bank", "Bank", "right"),
                                   col("difference", "Difference", "right"), col("invoices", "Invoice")]))
    d.summary = (f"{len(bad)} unprofitable customer(s) of {len(cust)}; discounts {dl['total_posted']} {cur} "
                 f"({dl['without_approval_claim']} with no approval claim; claims are unverified comments); cancellations cost about {cc['total_estimated']} {cur} (estimated); "
                 f"{len(disc)} payment(s) do not match the bank.")
