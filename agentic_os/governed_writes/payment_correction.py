"""The one ledger correction governed writes perform: a submitted payment the bank statement shows for another amount is
cancelled and entered again for the bank's amount.

ERPNext keeps the cancelled entry; the new one is its amendment. It is OFF unless a deployment sets
``AGENTIC_OS_PAYMENT_CORRECTIONS=1``, because it changes accounting records. It refuses whatever it was not shown: the books
and the bank are read again at the moment of writing and must still show exactly the approved figures; only the ONE
submitted payment under its reference, against ONE submitted bank deposit, received, allocated to exactly one sales
invoice, in one currency, with no deductions or taxes, is corrected. Anything else stays a finding for a person. Whether it worked is decided by ``economics.reconcile.corrections`` reading the application
again, never by the write's own answer.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Dict, List, Mapping

from agentic_os.economics import reconcile
from agentic_os.economics.contracts import money
from agentic_os.governed_writes.apps import NotApplied, OutcomeUnknown

ENV_ENABLED = "AGENTIC_OS_PAYMENT_CORRECTIONS"
#: what the amendment copies from the cancelled entry; every amount is set from the approved figures, never copied
COPIED = ("payment_type", "party_type", "party", "company", "posting_date", "paid_from", "paid_to", "paid_from_account_currency",
          "paid_to_account_currency", "mode_of_payment", "reference_no", "reference_date", "source_exchange_rate", "target_exchange_rate")


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "").strip() == "1"


class Reader:
    """The strict reader ``economics`` expects, over the writer's own connection: unreadable raises, it never reads empty."""

    def __init__(self, writer: Any) -> None:
        self.w = writer

    def read_list(self, doctype: str, fields: List[str], filters: Any = None, limit: int = 0) -> List[dict]:
        return self.w.read_list(doctype, fields, filters, limit)


def discrepancy_for(client: Any, reference: str):
    """The one open discrepancy under this reference. Raises when it cannot be established."""
    found = reconcile.find(client)
    if found.gaps:
        raise NotApplied("the books or the bank statement could not be read completely: " + "; ".join(str(g) for g in found.gaps)[:300])
    mine = [d for d in found.events if d.reference == reference]
    if len(mine) > 1:
        raise NotApplied("more than one payment shares this reference: a person has to sort it out")
    return mine[0] if mine else None


def reference_problem(client: Any, reference: str, payment: str, bank: Decimal) -> str:
    """Why the books and bank under this reference are not the simple case ("" = they are). A discrepancy is found by
    comparing EACH payment with the SUM of the bank lines under its reference, so re-entering a payment for that sum is
    only right when this payment is the only one and the sum is one received line: with a second payment it would count
    the money twice, and a fee or refund line netted into the sum is not a smaller payment."""
    try:
        pays = client.read_list("Payment Entry", ["name", "docstatus", "reference_no"], None, 0)
        lines = client.read_list("Bank Transaction", ["name", "deposit", "withdrawal", "reference_number", "docstatus"], None, 0)
    except Exception as e:  # noqa: BLE001
        raise NotApplied(f"the books or the bank statement could not be read ({type(e).__name__})") from None
    live = sorted(str(p.get("name")) for p in pays if int(p.get("docstatus") or 0) == 1 and str(p.get("reference_no") or "") == reference)
    if live != [payment]:
        return f"{len(live)} submitted payments carry this reference: which of them the bank's money belongs to is for a person to decide"
    mine = [b for b in lines if str(b.get("reference_number") or "") == reference and int(b.get("docstatus") or 0) != 2]
    if len(mine) != 1:
        return f"{len(mine)} bank lines carry this reference: a total of several lines (a fee, a refund, two receipts) is not one payment"
    line = mine[0]
    if int(line.get("docstatus") or 0) != 1:
        return "the bank line is not submitted"
    if money(line.get("withdrawal")) != 0 or money(line.get("deposit")) != bank:
        return "the bank line is not a single deposit of that amount"
    return ""


def refusal_for(doc: Mapping[str, Any], posted: Decimal) -> str:
    """Why this payment entry is not one this action corrects ("" = it is)."""
    refs = [r for r in (doc.get("references") or [])]
    reasons = (("it is not a submitted entry", doc.get("docstatus") != 1),
               ("it is not a received payment", doc.get("payment_type") != "Receive"),
               ("it is not allocated to exactly one sales invoice", len(refs) != 1 or refs[0].get("reference_doctype") != "Sales Invoice"),
               ("its amounts differ between the two accounts (another currency, or charges)",
                money(doc.get("paid_amount")) != money(doc.get("received_amount"))),
               ("it carries deductions or taxes", bool(doc.get("deductions")) or bool(doc.get("taxes"))),
               ("its amount is not the one in the proposal", money(doc.get("paid_amount")) != posted))
    return "; ".join(why for why, bad in reasons if bad)


def perform(writer: Any, i: Mapping[str, Any], footer: str, rid: str):
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    if not enabled():
        raise NotApplied(f"payment corrections are not enabled on this deployment ({ENV_ENABLED})")
    reference, posted, bank = str(i["reference"]), money(i["posted_amount"]), money(i["bank_amount"])
    client = Reader(writer)
    open_ = discrepancy_for(client, reference)
    if open_ is None:
        done = next((c for c in reconcile.corrections(client).events if c.reference == reference), None)
        if done is not None and done.reconciled and done.replacement_amount == bank:
            return Outcome(False, True, done.replacement_payment, "the books already agree with the bank for this reference",
                           "already corrected; nothing was changed")
        raise NotApplied("the books and the bank no longer disagree under this reference")
    if (open_.payment, open_.posted, open_.bank) != (str(i["payment_entry"]), posted, bank):
        raise NotApplied("the figures changed since the proposal: propose it again from the current discrepancy")
    if bank <= 0:
        raise NotApplied("the bank shows no positive amount: a person has to look at this")
    why = reference_problem(client, reference, open_.payment, bank)
    if why:
        raise NotApplied(f"this payment is not corrected automatically: {why}")
    doc = writer.document("Payment Entry", open_.payment)
    why = refusal_for(doc, posted)
    if why:
        raise NotApplied(f"this payment is not corrected automatically: {why}")
    line = (doc.get("references") or [])[0]
    allocated = min(bank, money(line.get("allocated_amount")))
    replacement: Dict[str, Any] = {k: doc[k] for k in COPIED if doc.get(k) not in (None, "")}
    replacement.update(amended_from=open_.payment, paid_amount=float(bank), received_amount=float(bank),
                       remarks=f"Entered again for the amount the bank shows. {footer}"[:1000],
                       references=[{"reference_doctype": line["reference_doctype"], "reference_name": line["reference_name"],
                                    "allocated_amount": float(allocated)}])
    try:
        writer.cancel("Payment Entry", open_.payment)                    # from here on the books have changed
    except OutcomeUnknown as e:
        raise OutcomeUnknown(f"CHECK NOW: the cancellation of Payment Entry {open_.payment} was not confirmed ({e}); no replacement was "
                             "entered. Look at it in ERPNext before anything else.") from None
    try:
        made = writer.create_submitted("Payment Entry", replacement)
    except (NotApplied, OutcomeUnknown) as e:
        # NOT "not applied": the original is cancelled. A person must record the payment; it is never retried by itself.
        raise OutcomeUnknown(f"CHECK NOW: Payment Entry {open_.payment} is cancelled and its replacement for {bank} was not "
                             f"confirmed ({e}). Look at Payment Entry in ERPNext before anything else.") from None
    try:
        done = next((c for c in reconcile.corrections(client).events if c.reference == reference), None)
    except Exception as e:  # noqa: BLE001
        return Outcome(True, False, made, f"written, but it could not be read back: {type(e).__name__}")
    ok = bool(done and done.reconciled and done.replacement_payment == made and done.replacement_amount == bank)
    return Outcome(True, ok, made, (f"{open_.payment} is cancelled; {made} records {bank}, which agrees with the bank; "
                                    f"{line['reference_name']} stays open for {posted - bank}") if ok else
                   f"{made} was created, but the books do not yet read as reconciled: look at both entries")


__all__ = ["ENV_ENABLED", "enabled", "perform", "discrepancy_for", "refusal_for", "Reader", "COPIED"]
