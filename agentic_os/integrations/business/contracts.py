"""Canonical business objects (plan §20). Provider-neutral, typed, evidence-preserving.

Money is always an integer in the currency's MINOR units (cents) — never a float — with an explicit
currency. Every object carries :class:`Provenance`: which provider it came from, the provider's own id
(what verification re-reads), evidence refs to the raw payload, and bitemporal timestamps —
``observed_at`` (when ReDevOps saw it) vs ``known_at`` (the provider's own fact time). The raw provider
payload is NOT inlined; it stays an evidence artifact referenced by ``evidence_refs``.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Mapping, Tuple


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Provenance:
    """Where a canonical object came from and when — never carries credential material."""
    provider: str
    provider_ref: str = ""                 # the provider's own object id (verification re-reads this)
    evidence_refs: Tuple[str, ...] = ()    # refs to raw-payload evidence artifacts
    observed_at: int = field(default_factory=now_ms)
    known_at: int = 0                      # provider fact time (ms); 0 = unknown

    def as_dict(self) -> dict:
        d = asdict(self)
        d["evidence_refs"] = list(self.evidence_refs)
        return d


@dataclass(frozen=True)
class BusinessObject:
    """Base for every canonical business object. ``prov`` is the only non-typed field; subclasses add
    the domain fields. ``digest`` content-addresses the TYPED fields (excluding provenance) so identical
    facts from different observations compare equal and changes are detectable."""
    prov: Provenance
    KIND: ClassVar[str] = "business.object"

    def business_fields(self) -> dict:
        d = asdict(self)
        d.pop("prov", None)
        return d

    def digest(self) -> str:
        canonical = json.dumps({"kind": self.KIND, **self.business_fields()},
                               sort_keys=True, default=str, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {"kind": self.KIND, "digest": self.digest(), "prov": self.prov.as_dict(),
                **self.business_fields()}


# ── identity / customer ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Party(BusinessObject):
    """A person or organization."""
    KIND: ClassVar[str] = "identity.party"
    kind_of: str = "person"                # person | organization
    name: str = ""
    email: str = ""
    external_ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Customer(BusinessObject):
    KIND: ClassVar[str] = "identity.customer"
    name: str = ""
    email: str = ""
    account_ref: str = ""


# ── CRM ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Account(BusinessObject):
    KIND: ClassVar[str] = "crm.account"
    name: str = ""
    domain: str = ""
    owner_ref: str = ""


@dataclass(frozen=True)
class Contact(BusinessObject):
    KIND: ClassVar[str] = "crm.contact"
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    account_ref: str = ""


@dataclass(frozen=True)
class Lead(BusinessObject):
    KIND: ClassVar[str] = "crm.lead"
    email: str = ""
    name: str = ""
    source: str = ""
    status: str = ""


@dataclass(frozen=True)
class Opportunity(BusinessObject):
    KIND: ClassVar[str] = "crm.opportunity"
    name: str = ""
    account_ref: str = ""
    stage: str = ""
    amount_cents: int = 0
    currency: str = ""
    close_date: str = ""


# ── communications ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Message(BusinessObject):
    KIND: ClassVar[str] = "comms.message"
    channel: str = ""                      # email | slack | whatsapp | …
    thread_ref: str = ""
    direction: str = "unknown"             # inbound | outbound | unknown
    from_ref: str = ""
    to_refs: Tuple[str, ...] = ()
    subject: str = ""
    snippet: str = ""


# ── invoices / receivables ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class InvoiceLine(BusinessObject):
    KIND: ClassVar[str] = "invoice.line"
    description: str = ""
    quantity: float = 1.0
    unit_amount_cents: int = 0
    amount_cents: int = 0
    currency: str = ""


@dataclass(frozen=True)
class Invoice(BusinessObject):
    KIND: ClassVar[str] = "invoice.invoice"
    number: str = ""
    customer_ref: str = ""
    amount_cents: int = 0
    amount_paid_cents: int = 0
    currency: str = ""
    status: str = ""                       # draft | open | paid | void | uncollectible
    issued_date: str = ""
    due_date: str = ""
    line_digests: Tuple[str, ...] = ()     # refs to InvoiceLine digests (lines are their own objects)


@dataclass(frozen=True)
class Payment(BusinessObject):
    KIND: ClassVar[str] = "invoice.payment"
    invoice_ref: str = ""
    amount_cents: int = 0
    currency: str = ""
    method: str = ""
    status: str = ""


@dataclass(frozen=True)
class Receivable(BusinessObject):
    KIND: ClassVar[str] = "invoice.receivable"
    invoice_ref: str = ""
    customer_ref: str = ""
    amount_outstanding_cents: int = 0
    currency: str = ""
    days_overdue: int = 0


# ── payments (gateway) ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Charge(BusinessObject):
    KIND: ClassVar[str] = "payment.charge"
    amount_cents: int = 0
    currency: str = ""
    status: str = ""
    payment_intent_ref: str = ""
    refunded: bool = False
    amount_refunded_cents: int = 0


@dataclass(frozen=True)
class Refund(BusinessObject):
    KIND: ClassVar[str] = "payment.refund"
    charge_ref: str = ""
    amount_cents: int = 0
    currency: str = ""
    status: str = ""
    reason: str = ""


# ── support ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Ticket(BusinessObject):
    KIND: ClassVar[str] = "support.ticket"
    subject: str = ""
    status: str = ""
    priority: str = ""
    requester_ref: str = ""
    order_refs: Tuple[str, ...] = ()


# ── commerce ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Order(BusinessObject):
    KIND: ClassVar[str] = "commerce.order"
    number: str = ""
    customer_ref: str = ""
    total_cents: int = 0
    currency: str = ""
    status: str = ""
    item_count: int = 0
