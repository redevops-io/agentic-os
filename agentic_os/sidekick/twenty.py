"""TwentySurfaceAdapter — makes the Twenty CRM a first-class Sidekick surface.

Same portable model as Metabase (one Sidekick, many surfaces): the Twenty App side-panel reports what CRM object
the user is on and which records they've selected as a :class:`SurfaceContext`, so a follow-up ("why is this
deal stalled?", "prepare follow-ups for these 5") carries typed CRM context. Twenty's own **Apps framework**
(sandboxed React side-panel + MCP) hosts the panel — an **L3 extension, not a fork**. This module is the backend
seam the panel uses to shape context + deep links; it reuses the open-core sidekick contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

from .contracts import ArtifactLink, SurfaceContext

# Twenty's standard objects → the native route segment.
_OBJECT_ROUTE = {"opportunity": "opportunities", "company": "companies", "person": "people"}


@dataclass
class TwentySurfaceAdapter:
    """A Sidekick surface adapter for a Twenty instance + the user's current CRM object/selection."""
    base_url: str
    object_type: str = ""                 # "opportunity" | "company" | "person"
    object_id: str = ""
    selection: Tuple[str, ...] = ()       # record ids selected in a table/Kanban view
    filters: Mapping[str, Any] = field(default_factory=dict)
    view: str = ""                        # "record" | "table" | "kanban"
    surface_type: str = "twenty"

    def context(self) -> SurfaceContext:
        seg = _OBJECT_ROUTE.get(self.object_type, self.object_type)
        route = f"/object/{seg}/{self.object_id}" if self.object_id else (f"/objects/{seg}" if seg else "")
        return SurfaceContext(
            app_id="twenty", route=route, object_type=self.object_type,
            object_ids=(self.object_id,) if self.object_id else (), selection=self.selection,
            filters=dict(self.filters), view_state={"view": self.view} if self.view else {},
            native_capabilities=("crm.opportunity.read", "crm.contact.read", "crm.account.read",
                                 "crm.activity.write"))

    def deep_link(self, resource_type: str, resource_id: str) -> str:
        """Native Twenty record URL (opens the real CRM, same session)."""
        seg = _OBJECT_ROUTE.get(resource_type, resource_type)
        return f"{self.base_url.rstrip('/')}/object/{seg}/{resource_id}"

    def artifact_link(self, resource_type: str, resource_id: str, *, project_id: str = "",
                      session_id: str = "") -> ArtifactLink:
        return ArtifactLink(provider="twenty", resource_type=resource_type, resource_id=str(resource_id),
                            native_url=self.deep_link(resource_type, resource_id), project_id=project_id,
                            sidekick_session_id=session_id)


__all__ = ["TwentySurfaceAdapter"]
