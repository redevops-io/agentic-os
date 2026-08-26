"""ProjectionSeeder — project one world's entities into the app cores, idempotently (docx TABLE 6).

Turns a ``WorldEvent``'s canonical entities into `CanonicalObject`s and projects each into the OSS-backed apps
(Twenty, ERPNext, Chatwoot, Lago, Listmonk, Postiz) via the World Adapter layer, registering the id mappings
in the ``IdentityGraph`` so the same entity is visible as CRM context, support context, finance context and
governance evidence. Each app resolves to a configured + reachable real core, or the in-memory demo store —
and the seeder records which, with its realism, so the demo can show "projected into Twenty (SEEDED) / Lago
(LIVE)" honestly. The point is preserving source provenance while creating one coherent execution world.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from runtime_contracts.world import IdentityGraph, WorldEvent

from .adapters import AdapterRegistry
from .objects import APP_CATALOG, canonical_from_entity


class ProjectionSeeder:
    """Projects entities into the apps whose catalog covers each kind, via the AdapterRegistry. Pass
    ``adapters={app: adapter}`` to inject a core, or ``allow_real=False`` to force the in-memory demo store
    (the default keeps a demo running with no OSS core deployed)."""

    #: kept as a class attribute for back-compat; the live plan is the app catalog.
    DEFAULT_PLAN: Dict[str, Tuple[str, ...]] = APP_CATALOG

    def __init__(self, *, plan: Optional[Dict[str, Tuple[str, ...]]] = None,
                 adapters: Optional[Dict[str, Any]] = None, allow_real: bool = True) -> None:
        self._plan = plan or APP_CATALOG
        self.store: Dict[str, Any] = {}
        self._registry = AdapterRegistry(overrides=adapters or {}, store=self.store, allow_real=allow_real)
        self.projections: List[Dict[str, Any]] = []      # per-projection: app, native_id, adapter, realism

    def project(self, event: WorldEvent, graph: IdentityGraph) -> List[Tuple[str, str]]:
        """Project every carried entity into each app whose catalog covers its kind. Returns the (app,
        native_id) pairs created/confirmed. Idempotent — a re-seed of the same event is a no-op."""
        created: List[Tuple[str, str]] = []
        for ent in event.entity_ids:
            obj = canonical_from_entity(ent, event)
            for app in self._plan:
                if not (obj.kind in self._plan.get(app, ()) ):
                    continue
                adapter = self._registry.for_app(app)
                if not adapter.accepts(obj.kind):
                    continue
                native_id = adapter.upsert(obj)
                graph.register(ent.entity_id, app, native_id)
                created.append((app, native_id))
                self.projections.append({"app": app, "native_id": native_id, "adapter": adapter.name,
                                         "realism": adapter.realism, "kind": obj.kind,
                                         "canonical_id": obj.canonical_id})
        return created

    def record(self, app: str, native_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.for_app(app).get(native_id)

    def adapters_in_use(self) -> Dict[str, Dict[str, str]]:
        """Which adapter + realism each touched app resolved to — for the demo to label projections honestly."""
        return self._registry.resolved()
