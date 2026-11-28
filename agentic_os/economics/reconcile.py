"""Q5: can a financial discrepancy be traced, corrected, approved and reconciled?

This module does the first and the last step and prepares the middle two. It FINDS a payment the books recorded for one amount and the bank
statement shows for another, TRACES it through the payment to the invoice and the customer, and PROPOSES the correction as a typed object
that nothing applies. A correction is made by a person (or by a governed write once those are enabled) through the application, which keeps
the original document as a cancelled record; this module then confirms from the application's own records that the discrepancy is gone. It
never edits or deletes anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from .contracts import money
from .erpnext_events import _read
from .contracts import Extraction


@dataclass(frozen=True)
class Discrepancy:
    reference: str                 # the reference shared by the payment and the bank line (e.g. a wire reference)
    posted: Decimal                # what the books recorded
    bank: Decimal                  # what the bank shows
    difference: Decimal            # posted - bank
    payment: str                   # the payment entry
    invoices: Sequence[str]
    customer: str
    sources: Sequence[str]
    ambiguous: str = ""            # why no single payment can be named as the wrong one ("" = exactly one payment and one deposit under the reference)
    payments: Sequence[str] = ()   # every submitted payment under the reference

    def to_json(self) -> Dict[str, Any]:
        return {"reference": self.reference, "posted": str(self.posted), "bank": str(self.bank), "difference": str(self.difference), "payment": self.payment,
                "invoices": list(self.invoices), "customer": self.customer, "sources": list(self.sources), "ambiguous": self.ambiguous, "payments": list(self.payments)}


@dataclass(frozen=True)
class CorrectionProposal:
    """A proposal only. It names the document to correct, the evidence, and the effect; it carries no authority to apply anything."""
    discrepancy: Discrepancy
    action: str
    effect: str
    requires: str = "approval by a person with authority over the books"
    proposable: bool = True        # False: the evidence does not say which record is wrong, so no change is proposed

    def to_json(self) -> Dict[str, Any]:
        return {"action": self.action, "effect": self.effect, "requires": self.requires, "discrepancy": self.discrepancy.to_json(), "applied": False, "proposable": self.proposable}


def find(client: Any) -> Extraction:
    """Payments (submitted, not cancelled) against bank lines, grouped by the reference they share; returns an Extraction whose ``events`` are the discrepancies.

    The comparison is per REFERENCE: the payments' total against the bank lines' net. Exactly one submitted payment and one deposit under a reference is an unambiguous discrepancy
    that can be traced to a payment and proposed for correction. Several payments, several deposits, or a withdrawal (a fee or a reversal) under the same reference are compared by their totals
    and, when they disagree, reported as ``ambiguous`` with the reason: nothing says which record is wrong, and no correction is proposed (re-entering one of two payments for the bank's
    total would count the money twice; a fee line netted into the sum would read as a short payment)."""
    ex = Extraction()
    pays = _read(client, ex, "Payment Entry", ["name", "docstatus", "party", "paid_amount", "reference_no", "payment_type"], [["docstatus", "=", 1]])
    refs = _read(client, ex, "Payment Entry Reference", ["parent", "reference_doctype", "reference_name"], None)
    bank = _read(client, ex, "Bank Transaction", ["name", "deposit", "withdrawal", "reference_number", "docstatus"], None)
    if pays is None or bank is None:
        ex.gap("reconciliation", "payments or bank lines could not be read", "no statement about discrepancies can be made")
        return ex
    pays = [p for p in pays if int(p.get("docstatus") or 0) == 1]       # a cancelled entry is not a posting; do not rely on the server honouring the filter
    by_pay: Dict[str, List[str]] = {}
    for r in refs or []:
        if r.get("reference_doctype") == "Sales Invoice":
            by_pay.setdefault(str(r["parent"]), []).append(str(r["reference_name"]))
    lines: Dict[str, List[dict]] = {}
    for b in bank:
        if int(b.get("docstatus") or 0) != 2 and b.get("reference_number"):
            lines.setdefault(str(b["reference_number"]), []).append(b)
    ex.counts["Payment Entry"], ex.counts["Bank Transaction"] = len(pays), len(bank)
    groups: Dict[str, List[dict]] = {}
    for p in pays:
        if p.get("reference_no") and str(p["reference_no"]) in lines:
            groups.setdefault(str(p["reference_no"]), []).append(p)
    for ref, ps in sorted(groups.items()):
        ls = lines[ref]
        posted = sum((money(p.get("paid_amount")) for p in ps), Decimal("0.00"))
        net = sum((money(b.get("deposit")) - money(b.get("withdrawal")) for b in ls), Decimal("0.00"))
        if posted == net:
            continue
        inv = [i for p in ps for i in by_pay.get(str(p["name"]), [])]
        why = []
        if len(ps) > 1:
            why.append(f"{len(ps)} payments are submitted under this reference")
        if len(ls) > 1:
            why.append(f"{len(ls)} bank lines carry this reference")
        if any(money(b.get("withdrawal")) for b in ls):
            why.append("a bank line under this reference is a withdrawal (a fee or a reversal), not a short deposit")
        parties = {str(p.get("party") or "") for p in ps}
        if len(parties) > 1:
            why.append("the payments are from different parties")
        names = [str(p["name"]) for p in ps]
        ex.events.append(Discrepancy(ref, posted, net, posted - net, names[0] if len(ps) == 1 else "", inv, next(iter(parties)) if len(parties) == 1 else "", (*[f"erpnext:Payment Entry/{n}" for n in names], *[f"erpnext:Sales Invoice/{i}" for i in inv]),
                                     "; ".join(why), tuple(names)))
    return ex


def propose(d: Discrepancy) -> CorrectionProposal:
    if d.ambiguous or not d.payment:
        return CorrectionProposal(d, "none: no single payment can be named as the wrong one", f"{d.ambiguous or 'more than one payment or bank line'}; the totals differ by {d.difference}. "
                                  "A person has to decide which record is wrong before anything is cancelled or entered again.", "a person to decide which record is wrong", False)
    return CorrectionProposal(d, f"cancel Payment Entry {d.payment} and record it again for {d.bank} (the amount the bank shows)",
                              f"the books show {d.bank} received for {d.reference}; invoice(s) {', '.join(d.invoices)} stay open for {d.difference}, "
                              f"which {d.customer} still owes")


@dataclass(frozen=True)
class Correction:
    """A payment that was cancelled and recorded again for another amount: the history of a discrepancy that was dealt with. The cancelled entry stays in the application."""
    reference: str
    cancelled_payment: str
    cancelled_amount: Decimal
    replacement_payment: str
    replacement_amount: Decimal
    bank_amount: Optional[Decimal]
    invoices: Sequence[str]
    customer: str
    sources: Sequence[str]

    @property
    def reconciled(self) -> bool:
        return self.bank_amount is not None and self.bank_amount == self.replacement_amount

    def to_json(self) -> Dict[str, Any]:
        return {"reference": self.reference, "cancelled_payment": self.cancelled_payment, "cancelled_amount": str(self.cancelled_amount),
                "replacement_payment": self.replacement_payment, "replacement_amount": str(self.replacement_amount),
                "bank_amount": None if self.bank_amount is None else str(self.bank_amount), "reconciled": self.reconciled,
                "invoices": list(self.invoices), "customer": self.customer, "sources": list(self.sources)}


def corrections(client: Any) -> Extraction:
    """Payments cancelled and re-entered under the same reference with a different amount, and whether the replacement now agrees with the bank."""
    ex = Extraction()
    pays = _read(client, ex, "Payment Entry", ["name", "docstatus", "party", "paid_amount", "reference_no"], None)
    refs = _read(client, ex, "Payment Entry Reference", ["parent", "reference_doctype", "reference_name"], None)
    bank = _read(client, ex, "Bank Transaction", ["name", "deposit", "withdrawal", "reference_number", "docstatus"], None)
    if pays is None:
        return ex
    by_pay: Dict[str, List[str]] = {}
    for r in refs or []:
        if r.get("reference_doctype") == "Sales Invoice":
            by_pay.setdefault(str(r["parent"]), []).append(str(r["reference_name"]))
    lines: Dict[str, Decimal] = {}
    for b in bank or []:
        if int(b.get("docstatus") or 0) != 2 and b.get("reference_number"):
            k = str(b["reference_number"])
            lines[k] = lines.get(k, Decimal("0.00")) + money(b.get("deposit")) - money(b.get("withdrawal"))
    groups: Dict[str, List[dict]] = {}
    for p in pays:
        if p.get("reference_no"):
            groups.setdefault(str(p["reference_no"]), []).append(p)
    for ref, rows in sorted(groups.items()):
        live = [r for r in rows if int(r.get("docstatus") or 0) == 1]
        dead = [r for r in rows if int(r.get("docstatus") or 0) == 2]
        if len(live) != 1 or not dead:
            continue
        for d in dead:
            if money(d.get("paid_amount")) != money(live[0].get("paid_amount")):
                inv = by_pay.get(str(live[0]["name"]), []) or by_pay.get(str(d["name"]), [])
                ex.events.append(Correction(ref, str(d["name"]), money(d.get("paid_amount")), str(live[0]["name"]), money(live[0].get("paid_amount")), lines.get(ref),
                                            inv, str(live[0].get("party") or ""), (f"erpnext:Payment Entry/{d['name']}", f"erpnext:Payment Entry/{live[0]['name']}",
                                                                                   *[f"erpnext:Sales Invoice/{i}" for i in inv])))
    return ex
