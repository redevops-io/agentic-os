"""Shared base for Bring-Your-Own email delivery adapters (moat plan §3.11, §6 P1).

Email delivery is an execution: hand a message to a provider, get a normalized receipt. Every adapter follows the
same shape — entitled only when the tenant supplies a credential, one HTTP submit, a governed EmailDeliveryReceipt
with provenance/cost, and HTTP errors mapped onto EmailSubmitFailure. Subclasses implement `_submit` (method/url/
headers/body) and `_parse` (provider payload → status/message-id/reason). Fully offline-testable via injected fetch.
"""
from __future__ import annotations

import time
from typing import Optional

from runtime_contracts.protocol import (
    EmailDeliveryReceipt, EmailDeliveryStatus, EmailSendRequest, EmailSubmitFailure, content_hash,
)

from ..adapters._http import Fetch, http_json


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def submit_status_failure(status: int) -> Optional[EmailSubmitFailure]:
    if status in (401, 403):
        return EmailSubmitFailure.NOT_ENTITLED
    if status == 422:
        return EmailSubmitFailure.REJECTED           # unprocessable — content/sender/recipient policy
    if status == 429:
        return EmailSubmitFailure.RATE_LIMITED
    if status >= 500:
        return EmailSubmitFailure.UNAVAILABLE
    return None


class HttpDeliveryProvider:
    """Base BYO email delivery provider. Subclasses set provider_id/_price and implement _submit/_parse."""
    provider_id: str = "?"
    _price: float = 0.0

    def __init__(self, credential: str = "", fetch: Fetch = http_json):
        self._cred = credential
        self._fetch = fetch

    def check_entitlement(self, tenant: str) -> bool:
        return bool(self._cred)                        # BYO: no credential ⇒ not entitled

    def cost_estimate(self, request: EmailSendRequest) -> float:
        return self._price

    # ── subclass hooks ─────────────────────────────────────────────────────────────────────────────────
    def _submit(self, request: EmailSendRequest) -> "tuple[str, str, dict, dict]":
        """Return (method, url, headers, body) for the provider submit call."""
        raise NotImplementedError

    def _parse(self, body: dict, request: EmailSendRequest) -> "tuple[EmailDeliveryStatus, str, str]":
        """Return (status, provider_message_id, reason) from a successful (2xx) response."""
        raise NotImplementedError

    def _receipt(self, request: EmailSendRequest, *, status, provider_message_id="",
                 submit_failure=None, reason="", cost=0.0, digest="") -> EmailDeliveryReceipt:
        return EmailDeliveryReceipt(
            provider=self.provider_id, tenant=request.tenant, idempotency_key=request.idempotency_key,
            status=status, provider_message_id=provider_message_id, submit_failure=submit_failure,
            reason=reason, cost=cost, accepted_at=_now() if status == EmailDeliveryStatus.ACCEPTED else "",
            observed_at=_now(), raw_response_digest=digest)

    # ── template send ──────────────────────────────────────────────────────────────────────────────────
    def send(self, request: EmailSendRequest) -> EmailDeliveryReceipt:
        if not self._cred:
            return self._receipt(request, status=EmailDeliveryStatus.FAILED,
                                 submit_failure=EmailSubmitFailure.NOT_ENTITLED,
                                 reason=f"no {self.provider_id} credential")
        if request.max_cost and self._price > request.max_cost:
            return self._receipt(request, status=EmailDeliveryStatus.FAILED,
                                 submit_failure=EmailSubmitFailure.QUOTA_EXCEEDED,
                                 reason=f"cost {self._price} exceeds max_cost {request.max_cost}")
        method, url, headers, body = self._submit(request)
        try:
            status_code, resp = self._fetch(method, url, headers, body)
        except Exception as e:  # noqa: BLE001
            return self._receipt(request, status=EmailDeliveryStatus.FAILED,
                                 submit_failure=EmailSubmitFailure.UNAVAILABLE, reason=str(e))
        fail = submit_status_failure(status_code)
        digest = content_hash(resp or {})
        if fail:
            return self._receipt(request, status=EmailDeliveryStatus.FAILED, submit_failure=fail,
                                 reason=f"http {status_code}", digest=digest)
        status, pmid, reason = self._parse(resp or {}, request)
        return self._receipt(request, status=status, provider_message_id=pmid, reason=reason,
                             cost=self._price if status == EmailDeliveryStatus.ACCEPTED else 0.0, digest=digest)

    def poll(self, tenant: str, provider_message_id: str) -> Optional[EmailDeliveryReceipt]:
        """Default: no webhook-free reconciliation. Providers that support it override this."""
        return None
