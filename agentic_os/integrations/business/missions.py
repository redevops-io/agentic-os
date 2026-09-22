"""Phase C — the first cross-app business Mission over canonical contracts: Receivables.

This is the flagship demonstration (plan §21/§29/§58.2): reason across systems in CANONICAL terms —
invoices/receivables (accounting) + contacts (CRM) + correspondence (email) — to find the accounts that
actually matter, propose a governed follow-up, and (after approval) execute + verify through the
Integration-Plane runner. Provider-neutral: it operates only on :mod:`.contracts` objects and never
touches provider JSON, so the same Mission works over Stripe/QuickBooks/Xero + HubSpot/Salesforce +
Gmail/Outlook once those adapters normalize.

It is deliberately deterministic and explainable (plan §59): materiality is an explicit function of
amount and age, evidence links are explicit, and a proposal is a draft that is NOT sent until an
exact-content approval authorizes it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import BusinessObject, Contact, Customer, Message, Receivable


def _digest(obj) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ReceivableCandidate:
    """One overdue account, with the cross-system evidence that explains why it was surfaced."""
    receivable: Receivable
    contact: Optional[Contact]
    last_message: Optional[Message]
    materiality_cents: int
    reasons: Tuple[str, ...]

    @property
    def customer_key(self) -> str:
        return self.receivable.customer_ref or (self.contact.email if self.contact else "")

    @property
    def evidence_refs(self) -> Tuple[str, ...]:
        refs = [self.receivable.digest()]
        if self.contact is not None:
            refs.append(self.contact.digest())
        if self.last_message is not None:
            refs.append(self.last_message.digest())
        return tuple(refs)

    def explain(self) -> dict:
        return {"customer": self.customer_key, "amount_outstanding_cents": self.receivable.amount_outstanding_cents,
                "currency": self.receivable.currency, "days_overdue": self.receivable.days_overdue,
                "materiality_cents": self.materiality_cents, "reasons": list(self.reasons),
                "has_contact": self.contact is not None, "has_correspondence": self.last_message is not None,
                "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class FollowupProposal:
    """A drafted follow-up for a candidate — NOT sent. Approval binds this exact content
    (``content_digest``); editing the draft changes the digest and invalidates the authorization."""
    candidate: ReceivableCandidate
    channel: str                    # "email"
    to_ref: str
    subject: str
    body: str
    capability: str = "email.message.send"
    proposal_id: str = ""

    def __post_init__(self) -> None:
        if not self.proposal_id:
            object.__setattr__(self, "proposal_id", "fup_" + _digest(
                {"to": self.to_ref, "subj": self.subject})[7:19])

    def content_digest(self) -> str:
        return _digest({"channel": self.channel, "to": self.to_ref, "subject": self.subject,
                        "body": self.body, "capability": self.capability})

    def as_send_request(self) -> dict:
        """The provider-neutral request the runner passes to the channel adapter's send capability."""
        return {"to": self.to_ref, "subject": self.subject, "body": self.body,
                "content_digest": self.content_digest()}


def _index_by_key(objs: Iterable[BusinessObject], key_fn) -> dict:
    out: dict = {}
    for o in objs:
        k = key_fn(o)
        if k and k not in out:
            out[k] = o
    return out


def investigate_receivables(
    evidence: Sequence[BusinessObject], *, material_threshold_cents: int = 0,
    now_days: int = 0,
) -> Tuple[ReceivableCandidate, ...]:
    """Cross-system investigation over canonical evidence. Links each receivable to its CRM contact and
    most-recent correspondence by customer key (customer_ref / email), scores materiality = outstanding ×
    max(days_overdue, 1), keeps those at/above the threshold, and ranks by materiality (ties by key).

    Materiality is intentionally simple and explainable; a learned ranking (plan §59) is a later phase
    and must stay strategy-only."""
    receivables = [o for o in evidence if isinstance(o, Receivable)]
    contacts_by_email = _index_by_key([o for o in evidence if isinstance(o, Contact)],
                                      lambda c: c.email)
    contacts_by_ref = _index_by_key([o for o in evidence if isinstance(o, Contact)],
                                    lambda c: c.prov.provider_ref)
    customers_by_ref = _index_by_key([o for o in evidence if isinstance(o, Customer)],
                                     lambda c: c.prov.provider_ref)
    messages = [o for o in evidence if isinstance(o, Message)]

    candidates: List[ReceivableCandidate] = []
    for r in receivables:
        key = r.customer_ref
        contact = contacts_by_ref.get(key) or contacts_by_email.get(key)
        email = contact.email if contact else (customers_by_ref.get(key).email if customers_by_ref.get(key) else key)
        # most recent inbound/related message for this customer (by email in from/to/thread)
        related = [m for m in messages if email and (email in (m.from_ref,) + tuple(m.to_refs)
                                                     or m.thread_ref == key)]
        last_message = max(related, key=lambda m: m.prov.known_at or m.prov.observed_at, default=None)

        materiality = int(r.amount_outstanding_cents) * max(int(r.days_overdue), 1)
        if materiality < material_threshold_cents:
            continue
        reasons = [f"{r.amount_outstanding_cents} {r.currency} outstanding",
                   f"{r.days_overdue} days overdue"]
        if contact is None:
            reasons.append("no CRM contact linked — needs identity resolution")
        if last_message is not None:
            reasons.append("prior correspondence exists")
        candidates.append(ReceivableCandidate(r, contact, last_message, materiality, tuple(reasons)))

    candidates.sort(key=lambda c: (-c.materiality_cents, c.customer_key))
    return tuple(candidates)


def propose_followups(candidates: Sequence[ReceivableCandidate]) -> Tuple[FollowupProposal, ...]:
    """Draft one governed follow-up per candidate that has a reachable contact. Tone escalates with age;
    the message is a DRAFT — sending is a separate governed action requiring exact-content approval."""
    proposals: List[FollowupProposal] = []
    for c in candidates:
        if c.contact is None or not c.contact.email:
            continue                                    # unreachable → surfaced as a finding, not a send
        name = (c.contact.first_name or "there").strip()
        amount = f"{c.receivable.amount_outstanding_cents / 100:.2f} {c.receivable.currency.upper()}"
        if c.receivable.days_overdue >= 60:
            subject = f"Overdue balance of {amount} — let's resolve this"
            opener = "This balance is now significantly past due, and we'd like to help resolve it."
        else:
            subject = f"A quick note about your {amount} balance"
            opener = "We noticed this balance is past its due date."
        body = (f"Hi {name},\n\n{opener} If it's already on the way, thank you — please disregard. "
                f"Otherwise, can we help with a payment link or a short plan?\n\nBest,\nAccounts team")
        proposals.append(FollowupProposal(candidate=c, channel="email", to_ref=c.contact.email,
                                          subject=subject, body=body))
    return tuple(proposals)
