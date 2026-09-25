"""Governed email delivery (moat plan §3.11, §6 P1).

Email delivery is an EXECUTION, not read-only evidence, so it has its own contract (runtime_contracts email_delivery)
and its own bridge here — not the intelligence acquisition gate. Apps hand an EmailSendRequest to `send_email`,
which enforces suppression + idempotency, records receipts to Experience, and updates the suppression list. BYO
delivery providers (Postmark, SES) keep Listmonk's own sending unchanged when no credential is supplied.
"""
from .bridge import ReceiptStore, SuppressionList, send_email
from .providers import PostmarkProvider, SesProvider

__all__ = [
    "PostmarkProvider",
    "SesProvider",
    "ReceiptStore",
    "SuppressionList",
    "send_email",
]
