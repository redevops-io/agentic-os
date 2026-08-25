"""ProjectionSeeder — project one world's entities into the app cores, idempotently (docx TABLE 6).

Turns a ``WorldEvent``'s canonical entities into records in the OSS-backed apps (Twenty, ERPNext, Chatwoot,
Lago …) and registers the id mappings in the ``IdentityGraph`` so the same entity is visible as CRM
context, support context, finance context and governance evidence. The default implementation is an
in-memory demo store (idempotent, replay-stable); real adapters implement the same ``project_entity`` seam
against a live core. The point is preserving source provenance while creating one coherent execution world.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from runtime_contracts.world import EntityRef, IdentityGraph, WorldEvent


class ProjectionSeeder:
    """Projects entities into a set of named apps. Subclass / inject ``adapters`` to hit real cores; the
    base keeps an in-memory record per (app, native_id) so a demo runs with no OSS core deployed."""

    #: which canonical entity kinds each app should hold a record for (the projection plan)
    DEFAULT_PLAN: Dict[str, Tuple[str, ...]] = {
        "twenty": ("customer", "account", "contact", "opportunity", "vendor"),
        "erpnext": ("customer", "invoice", "payment", "opportunity"),
        "chatwoot": ("customer", "conversation", "ticket"),
        "lago": ("customer", "subscription", "invoice"),
    }

    def __init__(self, *, plan: "Dict[str, Tuple[str, ...]] | None" = None, adapters: "Dict[str, Any] | None" = None) -> None:
        self._plan = plan or self.DEFAULT_PLAN
        self._adapters = adapters or {}
        self.store: Dict[str, Dict[str, Any]] = {}          # f"{app}:{native_id}" -> record

    def project(self, event: WorldEvent, graph: IdentityGraph) -> List[Tuple[str, str]]:
        """Project every carried entity into each app whose plan covers its kind. Returns the (app,
        native_id) pairs created/confirmed. Idempotent — a re-seed of the same event is a no-op."""
        created: List[Tuple[str, str]] = []
        for ent in event.entity_ids:
            for app, kinds in self._plan.items():
                if ent.kind not in kinds:
                    continue
                native_id = self._project_entity(app, ent, event)
                graph.register(ent.entity_id, app, native_id)
                created.append((app, native_id))
        return created

    def _project_entity(self, app: str, ent: EntityRef, event: WorldEvent) -> str:
        adapter = self._adapters.get(app)
        if adapter is not None:                              # a real core adapter (duck-typed)
            return adapter.upsert(ent, event)
        # in-memory demo projection — deterministic id so replay is stable
        native_id = f"{app}-{ent.kind}-{ent.entity_id}"
        key = f"{app}:{native_id}"
        self.store.setdefault(key, {"app": app, "native_id": native_id, "kind": ent.kind,
                                    "entity_id": ent.entity_id, "label": ent.label,
                                    "source_record_id": event.source_record_id,
                                    "provenance": event.dataset_id, "realism": event.classification})
        return native_id

    def record(self, app: str, native_id: str) -> "Dict[str, Any] | None":
        return self.store.get(f"{app}:{native_id}")
