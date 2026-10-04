"""Integration plane — durable event ingestion (§5).

Webhooks are the trigger surface, but they are *hints, not truth*. The inbox: validate the signature → append
the raw event immutably → dedup by the provider's event id (effectively-once) → normalize → hand a trigger to a
workflow. The business transaction is NEVER executed inside the webhook handler — ingestion and execution are
decoupled, so a slow/failed downstream can't lose or double-run the event.

Pairs with cursors.py (webhook + periodic source reconciliation to catch lost events) and dlq.py (failures become
durable, replayable exceptions).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from runtime_contracts.protocol import content_hash


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def hmac_signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest of the raw body (the common webhook signing scheme)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_hmac(secret: str, body: bytes, provided: str) -> bool:
    """Constant-time comparison; tolerates a ``sha256=`` prefix."""
    if not provided:
        return False
    provided = provided.split("=", 1)[1] if provided.startswith("sha256=") else provided
    return hmac.compare_digest(hmac_signature(secret, body), provided)


@dataclass(frozen=True)
class RawEvent:
    """Append-only raw evidence of an inbound webhook — stored before anything is interpreted."""
    provider: str
    event_type: str
    event_id: str                      # the provider's event id, used for dedup
    body: bytes = b""
    received_at: str = field(default_factory=_now)
    headers: dict = field(default_factory=dict)

    @property
    def body_digest(self) -> str:
        return content_hash(self.body.decode("utf-8", "replace"))


@dataclass(frozen=True)
class NormalizedEvent:
    """A provider-neutral event a workflow trigger consumes."""
    provider: str
    trigger: str                       # e.g. "stripe.payout.created"
    object_type: str
    external_id: str
    fields: dict = field(default_factory=dict)
    raw_event_id: str = ""


# normalizer(raw) -> NormalizedEvent
Normalizer = Callable[[RawEvent], NormalizedEvent]


class EventRejected(Exception):
    """The webhook was rejected before ingestion (bad signature)."""


class EventInbox:
    """Append-only inbox with signature validation + idempotent dedup. In-memory with a persistence seam; a real
    deployment swaps the log/seen store for durable storage (the fold logic is unchanged)."""

    def __init__(self, *, secrets: Optional[dict[str, str]] = None):
        self._secrets = dict(secrets or {})        # provider -> signing secret ("" skips verification)
        self._log: list[RawEvent] = []
        self._seen: set[tuple[str, str]] = set()

    def ingest(self, provider: str, event_type: str, event_id: str, body: bytes,
               *, headers: Optional[dict] = None, signature_header: str = "x-signature") -> Optional[RawEvent]:
        """Validate signature → append → dedup. Returns the RawEvent, or ``None`` if it's a duplicate. Raises
        ``EventRejected`` on a bad signature. Does NOT execute anything."""
        headers = dict(headers or {})
        secret = self._secrets.get(provider, "")
        if secret:
            provided = headers.get(signature_header) or headers.get(signature_header.title(), "")
            if not verify_hmac(secret, body, provided):
                raise EventRejected(f"{provider}: invalid signature")
        key = (provider, event_id)
        if key in self._seen:
            return None                             # effectively-once: a replayed webhook is dropped
        raw = RawEvent(provider=provider, event_type=event_type, event_id=event_id, body=body, headers=headers)
        self._log.append(raw)
        self._seen.add(key)
        return raw

    def normalize(self, raw: RawEvent, normalizer: Normalizer) -> NormalizedEvent:
        return normalizer(raw)

    @property
    def events(self) -> list[RawEvent]:
        return list(self._log)

    def count(self, provider: str = "") -> int:
        return len([e for e in self._log if not provider or e.provider == provider])
