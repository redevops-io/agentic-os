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

    def to_json(self) -> Dict[str, Any]:
        return {"reference": self.reference, "posted": str(self.posted), "bank": str(self.bank), "difference": str(self.difference), "payment": self.payment,
                "invoices": list(self.invoices), "customer": self.customer, "sources": list(self.sources)}


@dataclass(frozen=True)
class CorrectionProposal:
    """A proposal only. It names the document to correct, the evidence, and the effect; it carries no authority to apply anything."""
    discrepancy: Discrepancy
    action: str
    effect: str
    requires: str = "approval by a person with authority over the books"

    def to_json(self) -> Dict[str, Any]:
        return {"action": self.action, "effect": self.effect, "requires": self.requires, "discrepancy": self.discrepancy.to_json(), "applied": False}


def find(client: Any) -> Extraction:
    """Payments (submitted, not cancelled) against bank lines by shared reference; returns an Extraction whose ``events`` are the discrepancies."""
    ex = Extraction()
    pays = _read(client, ex, "Payment Entry", ["name", "docstatus", "party", "paid_amount", "reference_no", "payment_type"], [["docstatus", "=", 1]])
    refs = _read(client, ex, "Payment Entry Reference", ["parent", "reference_doctype", "reference_name"], None)
    bank = _read(client, ex, "Bank Transaction", ["name", "deposit", "withdrawal", "reference_number", "docstatus"], None)
    if pays is None or bank is None:
        ex.gap("reconciliation", "payments or bank lines could not be read", "no statement about discrepancies can be made")
        return ex
    by_pay: Dict[str, List[str]] = {}
    for r in refs or []:
        if r.get("reference_doctype") == "Sales Invoice":
            by_pay.setdefault(str(r["parent"]), []).append(str(r["reference_name"]))
    lines: Dict[str, Decimal] = {}
    for b in bank:
        if int(b.get("docstatus") or 0) == 2 or not b.get("reference_number"):
            continue
        lines[str(b["reference_number"])] = lines.get(str(b["reference_number"]), Decimal("0.00")) + money(b.get("deposit")) - money(b.get("withdrawal"))
    ex.counts["Payment Entry"], ex.counts["Bank Transaction"] = len(pays), len(bank)
    for p in pays:
        ref = str(p.get("reference_no") or "")
        if not ref or ref not in lines:
            continue
        posted = money(p.get("paid_amount"))
        if posted != lines[ref]:
            ex.events.append(Discrepancy(ref, posted, lines[ref], posted - lines[ref], str(p["name"]), by_pay.get(str(p["name"]), []), str(p.get("party") or ""),
                                         (f"erpnext:Payment Entry/{p['name']}", *[f"erpnext:Sales Invoice/{i}" for i in by_pay.get(str(p["name"]), [])])))
    return ex


def propose(d: Discrepancy) -> CorrectionProposal:
    return CorrectionProposal(d, f"cancel Payment Entry {d.payment} and record it again for {d.bank} (the amount the bank shows)",
                              f"the books show {d.bank} received for {d.reference}; invoice(s) {', '.join(d.invoices)} stay open for {d.difference}, "
                              f"which {d.customer} still owes")
