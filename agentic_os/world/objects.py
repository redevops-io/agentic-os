"""Canonical Business Objects — the shared vocabulary every World Adapter projects into (docx §"canonical
objects", v2 §"canonical objects").

A dataset world carries entities as `EntityRef`s of a canonical `EntityKind` (organization, customer,
invoice, ticket …). Before an adapter writes one into an app core it is normalized to a `CanonicalObject`: a
kind, a label, a bag of normalized attributes, and its provenance + realism. The same CanonicalObject maps
into Twenty as a company, into Lago as a customer, into Chatwoot as a contact — one identity, many app
schemas — which is exactly the "one world → many apps, one identity" property the projection layer exists for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from runtime_contracts.world import EntityRef, WorldEvent


@dataclass(frozen=True)
class CanonicalObject:
    """A business record in canonical form, independent of any app's schema."""
    canonical_id: str
    kind: str                       # an EntityKind value
    label: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    source_record_id: str = ""
    provenance: str = ""            # the world/dataset it came from
    realism: str = ""              # the world event's realism classification

    def native_id(self, app: str) -> str:
        """A deterministic id for this object inside an app, so a re-seed is idempotent + replay-stable."""
        return f"{app}-{self.kind}-{self.canonical_id}"

    def to_record(self, app: str, native_id: str) -> Dict[str, Any]:
        return {"app": app, "native_id": native_id, "kind": self.kind,
                "entity_id": self.canonical_id, "canonical_id": self.canonical_id, "label": self.label,
                "attributes": dict(self.attributes), "source_record_id": self.source_record_id,
                "provenance": self.provenance, "realism": self.realism}


def _attributes_for(ent: EntityRef, event: WorldEvent) -> Dict[str, Any]:
    """Normalize a handful of fields every app can use. Deliberately small + deterministic; a richer mapping
    lives in each adapter. Pulls a couple of shared payload hints when present (amount / email / status)."""
    attrs: Dict[str, Any] = {"name": ent.label}
    payload = getattr(event, "payload", {}) or {}
    for k in ("amount", "email", "status", "domain", "budget"):
        if k in payload and not isinstance(payload[k], float):   # floats are refused by rcv1 canonicalization
            attrs[k] = payload[k]
    return attrs


def canonical_from_entity(ent: EntityRef, event: WorldEvent) -> CanonicalObject:
    return CanonicalObject(canonical_id=ent.entity_id, kind=ent.kind, label=ent.label,
                           attributes=_attributes_for(ent, event), source_record_id=event.source_record_id,
                           provenance=event.dataset_id, realism=event.classification)


# The projection plan: which canonical kinds each OSS-core app holds a record for. Entity-holding apps only
# (Metabase/CrowdSec are queried/telemetry, not projection targets). Adding an app is a row here + an adapter.
APP_CATALOG: Dict[str, Tuple[str, ...]] = {
    "twenty":   ("organization", "person", "customer", "account", "contact", "opportunity", "vendor"),
    "erpnext":  ("customer", "invoice", "payment", "expense", "ledger_entry", "opportunity"),
    "chatwoot": ("customer", "contact", "conversation", "ticket"),
    "lago":     ("customer", "subscription", "invoice", "payment"),
    "listmonk": ("person", "contact", "customer"),
    "postiz":   ("organization", "product"),
}

#: app → (the OSS core it wraps, the business system it belongs to)
APP_META: Dict[str, Tuple[str, str]] = {
    "twenty":   ("Twenty CRM", "revenue_intelligence"),
    "erpnext":  ("ERPNext", "finance"),
    "chatwoot": ("Chatwoot", "customer_success"),
    "lago":     ("Lago", "finance"),
    "listmonk": ("Listmonk", "revenue_intelligence"),
    "postiz":   ("Postiz", "revenue_intelligence"),
}
