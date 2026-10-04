"""Integration plane — the common provider contract (Phase 0, §4).

One interface across every SaaS app, so WorkflowDefinitions bind to CAPABILITIES (``billing.invoice.create``),
not vendor-specific methods. Optional capabilities are explicit — we never pretend every provider has identical
semantics. Provider-specific error strings never become the workflow control plane; they map onto a small
normalized taxonomy. A reference ``InMemoryIntegrationProvider`` implements the contract for conformance tests
and local development (it is the "fake provider" the Phase-0 exit criterion runs against).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .contracts import Observation, Resource, _now


# ── normalized errors (§4.4) ─────────────────────────────────────────────────────────────────────────────
class IntegrationErrorCode(str, Enum):
    AUTH_EXPIRED = "auth_expired"
    AUTH_REVOKED = "auth_revoked"
    RATE_LIMITED = "rate_limited"
    OBJECT_NOT_FOUND = "object_not_found"
    CONFLICT = "conflict"
    VALIDATION_FAILED = "validation_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PARTIAL_SUCCESS = "partial_success"
    TIMEOUT = "timeout"
    DUPLICATE = "duplicate"
    PERMISSION_DENIED = "permission_denied"
    SCHEMA_CHANGED = "schema_changed"
    UNKNOWN = "unknown"


class IntegrationError(Exception):
    def __init__(self, code: IntegrationErrorCode, detail: str = ""):
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ── capability taxonomy (§4.2) — the vendor-neutral verbs workflows bind to ──────────────────────────────
CAPABILITIES: frozenset[str] = frozenset({
    "crm.contact.read", "crm.contact.update", "crm.contact.create",
    "crm.opportunity.read", "crm.opportunity.update",
    "billing.invoice.read", "billing.invoice.create",
    "billing.payment.read", "billing.subscription.read", "billing.subscription.create",
    "accounting.journal.create", "accounting.ledger.read",
    "support.ticket.read", "support.ticket.comment", "support.ticket.update",
    "messaging.message.post", "messaging.message.read",
    "identity.user.disable", "identity.user.provision",
    "object.read", "object.create", "object.update",   # generic fallbacks
})


def is_known_capability(cap: str) -> bool:
    return cap in CAPABILITIES


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    external_id: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    error: "IntegrationErrorCode | None" = None
    detail: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    healthy: bool
    auth_ok: bool = True
    detail: str = ""
    checked_at: str = field(default_factory=_now)


@runtime_checkable
class IntegrationProvider(Protocol):
    """The common provider interface. A real connector additionally handles auth refresh, pagination, rate
    limits, retries, webhook validation, cursors, schema metadata and evidence capture (§4.3)."""
    provider: str

    def describe_capabilities(self) -> tuple[str, ...]: ...
    def health(self) -> ProviderHealth: ...

    # reads → evidence
    def read_object(self, object_type: str, external_id: str) -> Observation | None: ...
    def search_objects(self, object_type: str, query: dict) -> list[Observation]: ...

    # writes → actions
    def create_object(self, object_type: str, fields: dict, *, idempotency_key: str = "") -> ActionResult: ...
    def update_object(self, object_type: str, external_id: str, fields: dict,
                      *, idempotency_key: str = "") -> ActionResult: ...
    def execute_action(self, capability: str, inputs: dict, *, idempotency_key: str = "") -> ActionResult: ...

    # read-after-write verification (the heart of the Obligation)
    def verify_action(self, object_type: str, external_id: str) -> Observation | None: ...


class InMemoryIntegrationProvider:
    """Reference provider: an in-memory SaaS stand-in. Holds objects per ``object_type``; writes mutate them;
    verify_action reads them back. Deduped on idempotency_key (effectively-once). A ``drop_writes`` flag
    simulates the silent-failure case — the write 'succeeds' but nothing lands, so the Obligation catches it."""

    def __init__(self, provider: str, *, capabilities: tuple[str, ...] = (), drop_writes: bool = False,
                 healthy: bool = True):
        self.provider = provider
        self._caps = capabilities or ("object.read", "object.create", "object.update")
        self._drop = drop_writes
        self._healthy = healthy
        self._store: dict[str, dict[str, dict]] = {}      # object_type -> external_id -> fields
        self._seen: dict[str, ActionResult] = {}          # idempotency_key -> result
        self.calls: list[tuple[str, str]] = []            # (op, object_type)
        self._seq = 0

    # ── capability / health ──
    def describe_capabilities(self) -> tuple[str, ...]:
        return tuple(self._caps)

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider, healthy=self._healthy, auth_ok=self._healthy)

    def _resource(self) -> Resource:
        return Resource(provider=self.provider, capabilities=tuple(self._caps))

    def _obs(self, object_type: str, external_id: str, fields: dict) -> Observation:
        return Observation(resource_id=self.provider, object_type=object_type, external_id=external_id,
                           normalized_fields=dict(fields), known_at=_now())

    # ── reads ──
    def read_object(self, object_type: str, external_id: str) -> Observation | None:
        self.calls.append(("read", object_type))
        fields = (self._store.get(object_type) or {}).get(external_id)
        return self._obs(object_type, external_id, fields) if fields is not None else None

    def search_objects(self, object_type: str, query: dict) -> list[Observation]:
        self.calls.append(("search", object_type))
        out = []
        for ext, fields in (self._store.get(object_type) or {}).items():
            if all(fields.get(k) == v for k, v in query.items()):
                out.append(self._obs(object_type, ext, fields))
        return out

    # ── writes ──
    def _write(self, op: str, object_type: str, external_id: str, fields: dict, idempotency_key: str) -> ActionResult:
        self.calls.append((op, object_type))
        if idempotency_key and idempotency_key in self._seen:
            return self._seen[idempotency_key]                 # effectively-once
        if not self._healthy:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_UNAVAILABLE, self.provider)
        res = ActionResult(ok=True, external_id=external_id, fields=dict(fields), idempotency_key=idempotency_key)
        if not self._drop:                                     # drop_writes → the silent failure
            self._store.setdefault(object_type, {})[external_id] = dict(fields)
        if idempotency_key:
            self._seen[idempotency_key] = res
        return res

    def create_object(self, object_type: str, fields: dict, *, idempotency_key: str = "") -> ActionResult:
        self._seq += 1
        ext = fields.get("id") or f"{object_type}-{self._seq}"
        return self._write("create", object_type, ext, fields, idempotency_key)

    def update_object(self, object_type: str, external_id: str, fields: dict,
                      *, idempotency_key: str = "") -> ActionResult:
        existing = (self._store.get(object_type) or {}).get(external_id, {})
        return self._write("update", object_type, external_id, {**existing, **fields}, idempotency_key)

    def execute_action(self, capability: str, inputs: dict, *, idempotency_key: str = "") -> ActionResult:
        object_type = inputs.get("object_type", capability.split(".")[1] if "." in capability else "object")
        ext = inputs.get("external_id") or inputs.get("fields", {}).get("id", "")
        fields = inputs.get("fields", {})
        if ext and (self._store.get(object_type) or {}).get(ext) is not None:
            return self.update_object(object_type, ext, fields, idempotency_key=idempotency_key)
        return self.create_object(object_type, fields, idempotency_key=idempotency_key)

    # ── verification ──
    def verify_action(self, object_type: str, external_id: str) -> Observation | None:
        return self.read_object(object_type, external_id)
