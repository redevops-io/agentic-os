"""Inbound signal → validated Contact-upsert proposal (Stage E, 2026-11-16).

Closes the first hop of `Inbound event -> intent classification -> validated Contact upsert ->
outcome receipt -> existing learning pipeline`: a real inbound sender (Chatwoot conversation, or any
other source that can supply a name/email/message) is classified with the existing
`revenue.intent.classify_intent`, and — only when the sender looks like a real new commercial contact —
turned into a `ContactUpsertProposal` for the approval-gated `integrations.twenty_operator` to act on.

"Validated" here means what the owner's workflow description means: the record is well-formed before it
ever reaches Twenty (a real name, a syntactically real email, a confidence floor on the classification) —
not that the contact is confirmed correct. The approval gate downstream is the actual correctness check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from agentic_os.revenue.intent import IntentClassification, classify_intent

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: below this confidence, the signal is too weak to propose a CRM write — leave it as an observation only.
MIN_CONFIDENCE = 0.6


@dataclass(frozen=True)
class ContactUpsertProposal:
    name: str
    email: str
    source: str
    source_ref: str
    intent: str
    confidence: float
    rationale: str


def _valid_name(name: str) -> bool:
    return bool(name and name.strip() and len(name.strip()) >= 2)


def _valid_email(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


def propose_contact_from_classification(ic: IntentClassification, *, name: str,
                                        email: str) -> Optional[ContactUpsertProposal]:
    """Pure: given an already-classified inbound message plus the sender's claimed name/email, decide
    whether it's worth proposing a Contact upsert. Returns `None` (abstain) when the name/email aren't
    well-formed or the classification is `GENERAL`/too low-confidence to act on — the same
    `Optional[...]` abstain convention `revenue.leakage`'s detectors use."""
    if not _valid_name(name) or not _valid_email(email):
        return None
    if ic.confidence < MIN_CONFIDENCE:
        return None
    if ic.intent.value == "GENERAL":
        return None
    return ContactUpsertProposal(
        name=name.strip(), email=email.strip().lower(), source=ic.source, source_ref=ic.source_ref,
        intent=ic.intent.value, confidence=ic.confidence,
        rationale=f"{ic.source}:{ic.source_ref} classified {ic.intent.value} (confidence {ic.confidence:.2f})")


def propose_contact(text: str, *, name: str, email: str, source: str = "chatwoot", source_ref: str = "",
                    observed_at_ms: int = 0) -> Optional[ContactUpsertProposal]:
    """Convenience wrapper: classify `text` first, then `propose_contact_from_classification`."""
    ic = classify_intent(text, source=source, source_ref=source_ref, observed_at_ms=observed_at_ms)
    return propose_contact_from_classification(ic, name=name, email=email)
