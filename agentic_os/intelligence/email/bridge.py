"""Governed email delivery bridge (intelligence plan §3.11, §8, §10).

Apps (Listmonk) never call a delivery provider directly. They hand an EmailSendRequest to `send_email`, which:
  1. refuses to send to a suppressed recipient (governed side effect of prior bounce/complaint/unsubscribe);
  2. enforces idempotency — a key already delivered is not sent again;
  3. records every receipt to an append-only ledger (Experience: campaign → delivered → …);
  4. adds the recipient to the suppression list when the receipt says so.

Dependency-light JSONL persistence, mirroring the EvidenceValueStore.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from agentic_os._platform.files import append_line as _append_line, fsync_enabled, locked_append
from runtime_contracts.protocol import (
    EmailDeliveryProvider, EmailDeliveryReceipt, EmailDeliveryStatus, EmailSendRequest, EmailSubmitFailure,
)

_log = logging.getLogger(__name__)


class SuppressionList:
    """Per-tenant suppressed recipients. Bounces/complaints/unsubscribes land here and block future sends."""
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._set: set[tuple[str, str]] = set()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        d = json.loads(line)
                        self._set.add((d["tenant"], d["recipient"]))

    def contains(self, tenant: str, recipient: str) -> bool:
        return (tenant, recipient) in self._set

    def add(self, tenant: str, recipient: str, reason: str = "") -> bool:
        key = (tenant, recipient)
        if key in self._set:
            return False
        _append_line(self.path, json.dumps({"tenant": tenant, "recipient": recipient, "reason": reason}), fsync=fsync_enabled())
        self._set.add(key)                      # in memory only once the record is on disk: a failed write must not look like a suppression
        return True


class ReceiptStore:
    """Append-only ledger of delivery receipts — the email arm of Experience (§8)."""
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    def append(self, receipt: EmailDeliveryReceipt) -> None:
        _append_line(self.path, json.dumps(receipt.canonical_form()), fsync=fsync_enabled())      # the idempotency record: forced to disk

    def preflight(self) -> None:
        """Prove a receipt can be recorded NOW (the directory exists, the file can be created and its lock taken) without writing one. Raises
        ``OSError`` / ``LockTimeout`` otherwise, so ``send_email`` can refuse to send what it could not record."""
        locked_append(self.path, b"")

    def _rows(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def delivered_keys(self, tenant: str) -> set[str]:
        """Idempotency keys already accepted/delivered for a tenant (so a resend is a no-op)."""
        ok = (EmailDeliveryStatus.ACCEPTED.value, EmailDeliveryStatus.DELIVERED.value)
        return {r["idempotency_key"] for r in self._rows()
                if r["tenant"] == tenant and r["status"] in ok}

    def summary(self) -> dict[str, dict]:
        """Per provider: sends, accepted, delivered, bounced, complained, unsubscribed, spend."""
        agg: dict[str, dict] = {}
        for r in self._rows():
            a = agg.setdefault(r["provider"], dict(sends=0, accepted=0, delivered=0, bounced=0,
                                                   complained=0, unsubscribed=0, spend=0.0))
            a["sends"] += 1
            a["spend"] += r.get("cost", 0.0)
            s = r["status"]
            if s == "accepted":
                a["accepted"] += 1
            elif s == "delivered":
                a["delivered"] += 1
            elif s == "bounced":
                a["bounced"] += 1
            elif s == "complained":
                a["complained"] += 1
            elif s == "unsubscribed":
                a["unsubscribed"] += 1
        return agg


def send_email(
    provider: EmailDeliveryProvider,
    request: EmailSendRequest,
    *,
    receipts: Optional[ReceiptStore] = None,
    suppression: Optional[SuppressionList] = None,
) -> EmailDeliveryReceipt:
    """Send one message through the governed path. Suppression + idempotency are enforced before the provider is
    ever called, so a suppressed or duplicate send costs nothing."""
    tenant = request.tenant
    recipient = request.message.recipient

    if suppression is not None and suppression.contains(tenant, recipient):
        r = EmailDeliveryReceipt(
            provider=getattr(provider, "provider_id", "?"), tenant=tenant,
            idempotency_key=request.idempotency_key, status=EmailDeliveryStatus.DROPPED,
            submit_failure=EmailSubmitFailure.SUPPRESSED, reason="recipient on suppression list")
        if receipts is not None:
            receipts.append(r)
        return r

    if receipts is not None and request.idempotency_key in receipts.delivered_keys(tenant):
        return EmailDeliveryReceipt(
            provider=getattr(provider, "provider_id", "?"), tenant=tenant,
            idempotency_key=request.idempotency_key, status=EmailDeliveryStatus.ACCEPTED,
            reason="idempotent replay — already delivered", cost=0.0)

    if receipts is not None:
        receipts.preflight()                    # cannot record a receipt: fail BEFORE sending, never a send without a record
    receipt = provider.send(request)
    # The message is out. From here a failure to record must not turn into an exception the caller retries (that would send it twice): it is
    # logged at ERROR with the whole record, so the receipt and the suppression can be restored from the log.
    if receipts is not None:
        try:
            receipts.append(receipt)
        except Exception:  # noqa: BLE001
            _log.exception("email SENT but its receipt could not be recorded; record: %s", json.dumps(receipt.canonical_form()))
    if suppression is not None and receipt.should_suppress():
        try:
            suppression.add(tenant, recipient, reason=receipt.status.value)
        except Exception:  # noqa: BLE001
            _log.exception("email receipt says suppress but the suppression could not be recorded: tenant=%s recipient=%s reason=%s",
                           tenant, recipient, receipt.status.value)
    return receipt
