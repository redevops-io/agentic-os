"""An independent bank statement against the books' payments.

``reconcile.find`` compares a payment with the application's own Bank Transaction rows. Those rows are the application's copy; this module reads the statement the bank itself issued
(a CSV export) and compares it with the payments the books posted. It decides nothing about who is right: it names what agrees, what does not and WHY it may not, in eight kinds:

* ``matched``: same reference, same amount, same day.
* ``timing_difference``: same reference and amount, on different days; reported with the number of days and whether it crosses a month end.
* ``matched_with_fee``: the deposit plus the bank's fee line(s) for that reference equal the payment, to the cent. A fee is attributed only when the arithmetic closes.
* ``split_payment``: several deposits with one reference add up to the payment (all of it arrived, in pieces) or fall short of it (``partial_payment``, with what is outstanding).
* ``amount_mismatch``: same reference, different amount, nothing above explains it.
* ``reversal_not_in_books``: the bank returned money (a debit that names a payment's reference) and the books still hold the payment as received.
* ``unmatched_bank``: a statement line no payment accounts for (not a fee or reversal already attributed).
* ``unmatched_book``: a payment no statement line shows; ``in_transit`` instead when it is dated after the statement's last day.

Strict parsing: a row that cannot be read is a recorded gap, never dropped and never zero. References are compared after trimming and upper-casing, nothing fuzzier.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import Extraction, money
from .erpnext_events import _read

FEE = re.compile(r"\b(?:fee|fees|charge|charges|commission|wire cost|bank cost)\b", re.I)
REVERSAL = re.compile(r"\b(?:rev(?:ersal)?|return(?:ed)?|rtn|chargeback|recall)\b[-:/ #]*([A-Za-z0-9][A-Za-z0-9\-_/]*)", re.I)
LATE_DAYS = 5


@dataclass(frozen=True)
class StatementLine:
    id: str                    # "<file>:<row number>"
    date: str                  # ISO
    amount: Decimal            # credits positive, debits negative
    currency: str
    reference: str
    description: str


@dataclass(frozen=True)
class Posting:
    name: str                  # the Payment Entry
    date: str
    amount: Decimal            # received amount (positive)
    currency: str
    reference: str
    party: str
    invoices: Sequence[str] = ()
    unallocated: Optional[Decimal] = None   # what the payment holds that no invoice has been allocated (None = not read)


@dataclass(frozen=True)
class BankFinding:
    kind: str
    reference: str
    posting: Optional[str] = None
    lines: Sequence[str] = ()
    booked: Optional[Decimal] = None
    banked: Optional[Decimal] = None
    difference: Optional[Decimal] = None
    note: str = ""
    through: str = ""          # the statement's last day (ISO), for findings that depend on how far the statement runs
    latest: str = ""           # the latest deposit date among ``lines``

    def to_json(self) -> Dict[str, Any]:
        d = {"kind": self.kind, "reference": self.reference, "posting": self.posting, "lines": list(self.lines), "note": self.note, "through": self.through, "latest": self.latest}
        for k in ("booked", "banked", "difference"):
            v = getattr(self, k)
            d[k] = str(v) if v is not None else None
        return d


def _ref(v: Any) -> str:
    return re.sub(r"\s+", "", str(v or "")).upper()


def _date(v: str) -> Optional[str]:
    try:
        return _dt.date.fromisoformat(v.strip()).isoformat()
    except ValueError:
        return None


def parse_statement(text: str, name: str, ex: Extraction, *, currency: str = "USD") -> List[StatementLine]:
    """CSV with a header. Columns (case-insensitive): ``date`` (ISO), ``amount`` (signed) OR ``deposit`` and ``withdrawal``, ``reference``, ``description``, optional ``currency``."""
    out: List[StatementLine] = []
    rd = csv.DictReader(io.StringIO(text))
    if not rd.fieldnames:
        ex.gap(f"bank:{name}", "empty statement", "no bank evidence")
        return out
    cols = {c.strip().lower(): c for c in rd.fieldnames}
    if "date" not in cols or not ("amount" in cols or "deposit" in cols or "withdrawal" in cols):
        ex.gap(f"bank:{name}", f"header lacks a date or an amount column ({sorted(cols)})", "no bank evidence")
        return out
    for n, row in enumerate(rd, start=2):
        g = lambda k: (row.get(cols[k]) or "").strip() if k in cols else ""  # noqa: E731
        d = _date(g("date"))
        try:
            if "amount" in cols and g("amount"):
                amt = Decimal(g("amount").replace(",", ""))
            else:
                amt = Decimal(g("deposit").replace(",", "") or "0") - Decimal(g("withdrawal").replace(",", "") or "0")
        except InvalidOperation:
            amt = None
        if d is None or amt is None:
            ex.gap(f"bank:{name}:row {n}", "date or amount could not be read", "this line is missing from the comparison; the statement may be incomplete")
            continue
        out.append(StatementLine(f"{name}:{n}", d, money(amt), (g("currency") or currency).upper(), _ref(g("reference")), g("description")))
    ex.counts["Bank statement line"] = ex.counts.get("Bank statement line", 0) + len(out)
    return out


def posting_from_entry(doc: Mapping[str, Any]) -> Posting:
    """A ``Posting`` from a Payment Entry document as ERPNext returns it (``name``, ``posting_date``, ``paid_amount``, ``reference_no``, ``party``, ``unallocated_amount``, and the
    ``references`` child rows). Used when the caller already holds the document and does not want ``read_postings`` to read it again."""
    inv = tuple(str(r.get("reference_name")) for r in (doc.get("references") or []) if r.get("reference_doctype") == "Sales Invoice" and r.get("reference_name"))
    return Posting(str(doc.get("name") or ""), str(doc.get("posting_date") or "")[:10], money(doc.get("paid_amount")), str(doc.get("paid_to_account_currency") or doc.get("currency") or "USD"),
                   _ref(doc.get("reference_no")), str(doc.get("party") or ""), inv, money(doc["unallocated_amount"]) if doc.get("unallocated_amount") is not None else None)


def read_postings(client: Any, ex: Extraction) -> List[Posting]:
    pays = _read(client, ex, "Payment Entry", ["name", "docstatus", "party", "paid_amount", "reference_no", "payment_type", "posting_date", "paid_to_account_currency", "unallocated_amount"], [["docstatus", "=", 1]])
    refs = _read(client, ex, "Payment Entry Reference", ["parent", "reference_doctype", "reference_name"], None)
    if pays is None:
        ex.gap("bank", "payments could not be read", "no comparison can be made")
        return []
    by_pay: Dict[str, List[str]] = {}
    for r in refs or []:
        if r.get("reference_doctype") == "Sales Invoice":
            by_pay.setdefault(str(r["parent"]), []).append(str(r["reference_name"]))
    return [Posting(str(p["name"]), str(p.get("posting_date") or "")[:10], money(p.get("paid_amount")), str(p.get("paid_to_account_currency") or "USD"), _ref(p.get("reference_no")),
                    str(p.get("party") or ""), tuple(by_pay.get(str(p["name"]), [])),
                    money(p["unallocated_amount"]) if p.get("unallocated_amount") is not None else None) for p in pays if int(p.get("docstatus") or 0) == 1 and p.get("payment_type") == "Receive"]


def _days(a: str, b: str) -> int:
    return abs((_dt.date.fromisoformat(a) - _dt.date.fromisoformat(b)).days)


def match(lines: Sequence[StatementLine], postings: Sequence[Posting]) -> List[BankFinding]:
    out: List[BankFinding] = []
    used: set = set()
    last = max((l.date for l in lines), default="")
    by_ref: Dict[str, List[StatementLine]] = {}
    for l in lines:
        by_ref.setdefault(l.reference, []).append(l)
    # a debit that names a payment's reference is a reversal of it
    rev_of: Dict[str, List[StatementLine]] = {}
    for l in lines:
        if l.amount < 0:
            m = REVERSAL.search(l.reference + " " + l.description) or (re.match(r"^(?:REV|RTN|RET)[-:/]?(.+)$", l.reference))
            if m:
                rev_of.setdefault(_ref(m.group(1)), []).append(l)
    for p in sorted(postings, key=lambda x: (x.date, x.name)):
        deposits = [l for l in by_ref.get(p.reference, []) if l.amount > 0] if p.reference else []
        fees = [l for l in lines if l.amount < 0 and l.id not in used and FEE.search(l.description) and p.reference and (l.reference in (p.reference, p.reference + "-FEE", "FEE-" + p.reference) or p.reference in _ref(l.description))]
        revs = [l for l in rev_of.get(p.reference, []) if l.currency == p.currency]
        ids = [l.id for l in deposits]
        total = sum((l.amount for l in deposits), Decimal("0.00"))
        for l in deposits:
            used.add(l.id)
        if deposits and any(l.currency != p.currency for l in deposits):
            out.append(BankFinding("amount_mismatch", p.reference, p.name, ids, p.amount, total, None, f"currency differs ({p.currency} in the books, {sorted({l.currency for l in deposits})} at the bank): not converted"))
            continue
        if not deposits:
            if last and p.date > last:
                out.append(BankFinding("in_transit", p.reference, p.name, (), p.amount, None, None, f"posted {p.date}, after the statement's last day {last}"))
            else:
                out.append(BankFinding("unmatched_book", p.reference, p.name, (), p.amount, None, None, "no statement line carries this payment's reference" if p.reference else "the payment has no reference to match on"))
            continue
        for r in revs:
            used.add(r.id)
            out.append(BankFinding("reversal_not_in_books", p.reference, p.name, [r.id], p.amount, total + r.amount, p.amount - (total + r.amount),
                                   f"the bank debited {-r.amount} on {r.date} naming this reference; the payment is still submitted in the books"))
        net_fees = sum((l.amount for l in fees), Decimal("0.00"))
        if len(deposits) > 1 and total - net_fees == p.amount:
            for f in fees:
                used.add(f.id)
            out.append(BankFinding("split_payment", p.reference, p.name, ids + [f.id for f in fees], p.amount, total, p.amount - total, f"{len(deposits)} deposits add up to the payment" + (" (after fee lines)" if fees else "")))
        elif len(deposits) > 1 and total < p.amount:
            out.append(BankFinding("partial_payment", p.reference, p.name, ids, p.amount, total, p.amount - total, f"{len(deposits)} deposits so far; {p.amount - total} of the payment has not arrived"))
        elif total == p.amount:
            gap = max(_days(p.date, l.date) for l in deposits) if p.date else 0
            if gap:
                month = any(p.date[:7] != l.date[:7] for l in deposits)
                out.append(BankFinding("timing_difference", p.reference, p.name, ids, p.amount, total, Decimal("0.00"),
                                       f"the bank shows it {gap} day(s) from the posting date" + ("; across a month end" if month else "") + ("; later than the usual few days" if gap > LATE_DAYS else "")))
            else:
                out.append(BankFinding("matched", p.reference, p.name, ids, p.amount, total, Decimal("0.00")))
        elif fees and total - net_fees == p.amount:
            for f in fees:
                used.add(f.id)
            out.append(BankFinding("matched_with_fee", p.reference, p.name, ids + [f.id for f in fees], p.amount, total, p.amount - total, f"deposit {total} plus bank fee(s) {-net_fees} equal the payment"))
        else:
            out.append(BankFinding("amount_mismatch", p.reference, p.name, ids, p.amount, total, p.amount - total, "the amounts differ and no fee line or further deposit closes the gap",
                                   last, max(l.date for l in deposits)))
    for l in lines:
        if l.id not in used:
            out.append(BankFinding("unmatched_bank", l.reference, None, [l.id], None, l.amount, None, f"{l.date}: {l.description or 'no description'}"))
    return out


def reconcile_statement(client: Any, statements: Mapping[str, str], *, currency: str = "USD") -> Tuple[List[BankFinding], Extraction]:
    ex = Extraction()
    lines: List[StatementLine] = []
    for name, text in statements.items():
        lines += parse_statement(text, name, ex, currency=currency)
    postings = read_postings(client, ex)
    ex.counts["Payment Entry (received)"] = len(postings)
    return match(lines, postings), ex
