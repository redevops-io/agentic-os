"""Is this difference explained by a fee, an exchange rate, a partial settlement, an allocation, a reversal or timing?

A payment the books hold for one amount and the bank for another is not, by itself, evidence that the payment is wrong. This module classifies the difference from INDEPENDENT evidence only
(the bank's own statement, read by ``bank.reconcile_statement``, and the payment's own allocation) and says, per possible cause, whether it is ``supported``, ``excluded`` or ``unknown``.
A customer saying that a fee was deducted is a ``claimed`` fee: it is shown, it explains nothing, and it never makes the difference ``explained``.

``Explanation.corroborated`` is True only when the bank's statement independently shows a different amount for the reference AND no cause that could account for the difference is
supported or unknown (a customer's CLAIM of a fee does not stop it: the statement says what arrived, the claim only says who bears the difference, and it stays a claim). It is a classification, not an authorization: whoever acts on it (a governed write with an approver) decides. Without a statement nothing is corroborated: every cause is ``unknown``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import datetime as _dt

from .bank import LATE_DAYS, BankFinding, Posting

KINDS = ("fee", "fx", "partial_settlement", "allocation", "reversal", "timing")


@dataclass(frozen=True)
class Cause:
    kind: str
    status: str                            # supported | claimed | excluded | unknown
    amount: Optional[Decimal] = None       # how much of the difference it accounts for (supported only)
    evidence: Sequence[str] = ()           # statement lines, payments or communication ids
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "status": self.status, "amount": str(self.amount) if self.amount is not None else None, "evidence": list(self.evidence), "note": self.note}


@dataclass
class Explanation:
    reference: str
    difference: Decimal
    causes: List[Cause] = field(default_factory=list)
    residual: Decimal = Decimal("0.00")    # what no supported cause accounts for
    statement_seen: bool = False
    corroborated: bool = False

    @property
    def explained(self) -> bool:
        return self.statement_seen and self.residual == 0

    def to_json(self) -> Dict[str, Any]:
        return {"reference": self.reference, "difference": str(self.difference), "residual": str(self.residual), "statement_seen": self.statement_seen, "explained": self.explained,
                "corroborated": self.corroborated, "causes": [c.to_json() for c in self.causes]}


def explain_difference(reference: str, difference: Decimal, posting: Optional[Posting], statement: Sequence[BankFinding], claims: Sequence[Any] = ()) -> Explanation:
    """``statement`` are the ``bank.match`` findings (all of them; the ones for this reference are picked here). ``claims`` are communication edges or ids that STATE a fee or deduction
    (``correlate.correlate_discrepancies`` edges): they are recorded as a claimed fee, never counted."""
    mine = [f for f in statement if f.reference == reference]
    out = Explanation(reference, difference, statement_seen=bool(statement))
    if not statement:
        out.causes = [Cause(k, "unknown", note="no independent bank statement was read: nothing here can confirm or exclude this cause") for k in KINDS]
        out.residual = difference
        return out
    by = {f.kind: f for f in mine}
    supported = Decimal("0.00")

    def add(c: Cause) -> None:
        nonlocal supported
        out.causes.append(c)
        if c.status == "supported" and c.amount is not None:
            supported += c.amount

    f = by.get("matched_with_fee")
    claimed = [getattr(c, "communication", str(c)) for c in claims]
    if f:
        add(Cause("fee", "supported", abs(f.difference or Decimal("0.00")), f.lines, f.note))
    elif claimed:
        add(Cause("fee", "claimed", None, claimed, "a message states a fee or deduction; a statement line for it was not found, so it explains nothing"))
    else:
        add(Cause("fee", "excluded", None, [l for x in mine for l in x.lines], "no fee line under this reference on the statement"))
    cur = by.get("amount_mismatch")
    if cur and "currency" in cur.note:
        add(Cause("fx", "supported", None, cur.lines, cur.note + "; the amounts are not comparable, so the difference is not an amount"))
    else:
        add(Cause("fx", "excluded" if mine else "unknown", None, [], "the statement and the payment are in the same currency" if mine else "the reference is not on the statement"))
    p = by.get("partial_payment") or by.get("split_payment")
    if by.get("partial_payment"):
        add(Cause("partial_settlement", "supported", abs(p.difference or Decimal("0.00")), p.lines, p.note))
    else:
        short = by.get("amount_mismatch")
        if short and "currency" not in short.note and short.banked is not None and short.booked is not None and short.banked < short.booked:
            # one deposit smaller than the payment: a second instalment may still be on its way. The statement can exclude that only by running LATE_DAYS past both the posting and the deposit.
            need = max(posting.date if posting else "", short.latest or "")
            try:
                closed = bool(posting and short.through and short.latest and _dt.date.fromisoformat(short.through) >= _dt.date.fromisoformat(need) + _dt.timedelta(days=LATE_DAYS))
            except ValueError:
                closed = False
            if closed:
                add(Cause("partial_settlement", "excluded", None, short.lines, f"the statement runs to {short.through}, {LATE_DAYS}+ days past the payment and the deposit, and shows no further deposit under this reference"))
            else:
                add(Cause("partial_settlement", "unknown", None, short.lines, "one deposit is smaller than the payment and the statement does not run far enough past it to rule out a second instalment"))
        else:
            add(Cause("partial_settlement", "excluded" if mine else "unknown", None, [l for x in mine for l in x.lines], "all of the payment arrived" if mine else "the reference is not on the statement"))
    if posting is not None and posting.unallocated not in (None, Decimal("0.00")):
        add(Cause("allocation", "supported", abs(posting.unallocated), [posting.name], f"the payment holds {posting.unallocated} not allocated to any invoice"))
    else:
        add(Cause("allocation", "excluded" if posting is not None and posting.unallocated is not None else "unknown", None, [posting.name] if posting else [], "fully allocated" if posting is not None and posting.unallocated is not None else "allocation was not read"))
    r = by.get("reversal_not_in_books")
    add(Cause("reversal", "supported", abs(r.difference or Decimal("0.00")), r.lines, r.note) if r else Cause("reversal", "excluded" if mine else "unknown", None, [], "no debit names this reference" if mine else "the reference is not on the statement"))
    t = by.get("timing_difference") or by.get("in_transit")
    add(Cause("timing", "supported", Decimal("0.00"), t.lines, t.note) if t else Cause("timing", "excluded" if mine else "unknown", None, [], "same day" if mine else "the reference is not on the statement"))
    out.residual = (difference - supported).copy_abs() if out.statement_seen else difference
    out.corroborated = bool(mine) and out.residual != 0 and not any(c.status in ("supported", "unknown") for c in out.causes) and bool(by.get("amount_mismatch")) and "currency" not in by["amount_mismatch"].note
    return out
