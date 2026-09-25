"""Governed email delivery bridge (moat plan §3.11, §8, §10).

Apps (Listmonk) never call a delivery provider directly. They hand an EmailSendRequest to `send_email`, which:
  1. refuses to send to a suppressed recipient (governed side effect of prior bounce/complaint/unsubscribe);
  2. enforces idempotency — a key already delivered is not sent again;
  3. records every receipt to an append-only ledger (Experience: campaign → delivered → …);
  4. adds the recipient to the suppression list when the receipt says so.

Dependency-light JSONL persistence, mirroring the EvidenceValueStore.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from runtime_contracts.protocol import (
    EmailDeliveryProvider, EmailDeliveryReceipt, EmailDeliveryStatus, EmailSendRequest, EmailSubmitFailure,
)


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
        self._set.add(key)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tenant": tenant, "recipient": recipient, "reason": reason}) + "\n")
        return True


class ReceiptStore:
    """Append-only ledger of delivery receipts — the email arm of Experience (§8)."""
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    def append(self, receipt: EmailDeliveryReceipt) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt.canonical_form()) + "\n")

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

    receipt = provider.send(request)
    if receipts is not None:
        receipts.append(receipt)
    if suppression is not None and receipt.should_suppress():
        suppression.add(tenant, recipient, reason=receipt.status.value)
    return receipt
