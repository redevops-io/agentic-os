"""Economic events: what a business operation did to money, with the records that prove it.

This is a read-only PROJECTION over the accounting records the applications already hold (ERPNext documents and their general
ledger). It is not a ledger and stores no balance: every event names its source records, and a correction is a later event that
points at the one it supersedes, never an edit or a deletion.

Two rules the whole package keeps:

* ``Basis.POSTED`` is an amount written on a submitted accounting document. ``Basis.ESTIMATED`` is anything derived (hours times a
  costing rate, revenue that a cancellation forwent). A total always reports the two apart.
* ``amount`` is the effect on profit: money in is positive, money out and revenue given up are negative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

CENT = Decimal("0.01")


def money(v: Any) -> Decimal:
    """A cent-rounded Decimal from whatever an application returned; ``None`` and the unreadable are zero (coverage notes say what was missing)."""
    try:
        return Decimal(str(v if v is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        return Decimal("0.00")


class Basis(str, Enum):
    POSTED = "posted"
    ESTIMATED = "estimated"


class Kind(str, Enum):
    SALE = "sale"                                  # revenue at list price on a submitted sales invoice
    DISCOUNT = "discount"                          # list price given up on that invoice (negative)
    RETURN = "return"                              # a credit note, at list price (negative)
    COST_OF_GOODS = "cost_of_goods"                # stock cost of what was sold (negative; positive on a return)
    SUPPLIER_COST = "supplier_cost"                # a submitted purchase invoice (negative)
    SUPPLIER_RETURN = "supplier_return"            # a debit note (positive)
    SUPPORT_COST = "support_cost"                  # time logged as support
    REWORK_COST = "rework_cost"                    # time logged as rework
    CANCELLATION = "cancellation"                  # a cancelled sales invoice; the revenue it forwent when nothing replaced it
    CANCELLATION_HANDLING = "cancellation_handling"  # time logged against a cancellation


@dataclass(frozen=True)
class Approval:
    """What a comment on the document CLAIMS about who allowed the change. It is not evidence of an approval: anyone who can comment in the application
    can write that text, and an agent that writes a governed comment writes the words itself. ``verified`` stays False for everything read from a comment;
    only a record the trusted side writes (a governed request's decided_by, action and input digest, or the application's own workflow or version
    history) could set it, and nothing here reads one yet. Absent means no claim was found, not that no approval happened."""
    id: str
    approver: str
    action: str = ""
    verified: bool = False
    source: str = "comment on the document (unverified claim)"


@dataclass(frozen=True)
class EconomicEvent:
    id: str
    kind: Kind
    basis: Basis
    amount: Decimal
    currency: str
    at: str                                        # ISO date of the document the event comes from
    subjects: Mapping[str, str] = field(default_factory=dict)   # customer, supplier, project, invoice ...
    sources: Tuple[str, ...] = ()                  # e.g. "erpnext:Sales Invoice/ACC-SINV-2026-00001"
    supersedes: Optional[str] = None               # the document this one corrects
    approval: Optional[Approval] = None
    note: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind.value, "basis": self.basis.value, "amount": str(self.amount), "currency": self.currency,
                "at": self.at, "subjects": dict(self.subjects), "sources": list(self.sources), "supersedes": self.supersedes,
                "approval": ({"id": self.approval.id, "approver": self.approval.approver, "action": self.approval.action, "verified": self.approval.verified,
                              "source": self.approval.source} if self.approval else None),
                "note": self.note}


@dataclass
class Extraction:
    """Events plus what could not be read. A gap is part of the answer: an unreadable source never reads as zero."""
    events: list = field(default_factory=list)
    gaps: list = field(default_factory=list)          # [{"source": ..., "what": ..., "effect": ...}]
    counts: Dict[str, int] = field(default_factory=dict)
    notes: list = field(default_factory=list)         # optional capability a site does not have (a custom field): said, but it does not make the read incomplete

    def note(self, source: str, what: str, effect: str) -> None:
        self.notes.append({"source": source, "what": what, "effect": effect})

    def gap(self, source: str, what: str, effect: str) -> None:
        self.gaps.append({"source": source, "what": what, "effect": effect})


def in_period(at: str, since: str, until: str) -> bool:
    """ISO dates compare as text. ``since`` and ``until`` are inclusive; an empty bound is open."""
    return (not since or at >= since) and (not until or at <= until)
