"""Concrete BYO email delivery adapters (moat plan §3.11 P0, §6 P1).

These close Listmonk's deliverability weakness without ReDevOps operating an MTA reputation network — the tenant
brings a mature delivery provider and we normalize its receipts. Postmark and SES are reference adapters; more
follow the same base. Offline-testable via the injected fetch seam.
"""
from __future__ import annotations

from runtime_contracts.protocol import EmailDeliveryStatus, EmailSendRequest

from ._base import HttpDeliveryProvider


class PostmarkProvider(HttpDeliveryProvider):
    """Postmark transactional/broadcast API. Auth via the server token header; ErrorCode 0 = accepted."""
    provider_id = "postmark"
    _price = 0.00125
    _BASE = "https://api.postmarkapp.com"

    def _submit(self, request: EmailSendRequest):
        m = request.message
        stream = m.stream or "broadcast"
        return "POST", f"{self._BASE}/email", {
            "X-Postmark-Server-Token": self._cred, "Accept": "application/json",
        }, {
            "From": f"noreply@{m.sender_domain}", "To": m.recipient,
            "Subject": m.subject_ref, "HtmlBody": m.body_ref, "MessageStream": stream,
            "Metadata": {k: v for k, v in request.metadata},
        }

    def _parse(self, body: dict, request: EmailSendRequest):
        # Postmark: ErrorCode 0 = success; 406 (inactive recipient) surfaces as a non-zero code in a 2xx envelope.
        code = body.get("ErrorCode", 0)
        if code == 0:
            return EmailDeliveryStatus.ACCEPTED, str(body.get("MessageID", "")), body.get("Message", "OK")
        if code == 406:                                   # inactive / suppressed recipient
            return EmailDeliveryStatus.DROPPED, "", body.get("Message", "inactive recipient")
        return EmailDeliveryStatus.FAILED, "", body.get("Message", f"postmark error {code}")


class SesProvider(HttpDeliveryProvider):
    """Amazon SES (v2 outbound email JSON API). Credential is a pre-signed session token supplied by the tenant's
    own AWS integration; we do not hold long-lived AWS keys in this contract."""
    provider_id = "ses"
    _price = 0.0001

    def __init__(self, credential: str = "", region: str = "us-east-1", fetch=None):
        super().__init__(credential=credential, **({"fetch": fetch} if fetch else {}))
        self._region = region
        self._base = f"https://email.{region}.amazonaws.com/v2/email/outbound-emails"

    def _submit(self, request: EmailSendRequest):
        m = request.message
        return "POST", self._base, {
            "Authorization": f"Bearer {self._cred}", "Accept": "application/json",
        }, {
            "FromEmailAddress": f"noreply@{m.sender_domain}",
            "Destination": {"ToAddresses": [m.recipient]},
            "Content": {"Simple": {"Subject": {"Data": m.subject_ref},
                                   "Body": {"Html": {"Data": m.body_ref}}}},
            "ConfigurationSetName": m.stream or "default",
        }

    def _parse(self, body: dict, request: EmailSendRequest):
        mid = body.get("MessageId", "")
        if mid:
            return EmailDeliveryStatus.ACCEPTED, str(mid), "accepted"
        return EmailDeliveryStatus.FAILED, "", body.get("message", "no MessageId in SES response")
