"""AnalyticsContext — the Metabase-specific surface context (§6).

Lets "why did this spike?" resolve against the active series/time-range and "contact those customers" resolve the
SELECTED population into entities. It embeds into a generic :class:`SurfaceContext` (as ``view_state``) so the
portable Sidekick shell handles it like any other surface, while analytics-aware code can read it back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

from .contracts import SurfaceContext


@dataclass(frozen=True)
class AnalyticsContext:
    """What the user is looking at / has selected inside Metabase."""
    dashboard_id: str = ""
    card_id: str = ""
    dashboard_filters: Mapping[str, Any] = field(default_factory=dict)
    selected_entities: Tuple[str, ...] = ()       # resolved entity refs (e.g. customer ids) behind the selection
    selected_rows: Tuple[Mapping[str, Any], ...] = ()
    selected_chart_region: Mapping[str, Any] = field(default_factory=dict)   # e.g. a time range / series
    question_metadata: Mapping[str, Any] = field(default_factory=dict)
    dataset_metadata: Mapping[str, Any] = field(default_factory=dict)
    source_lineage: Tuple[str, ...] = ()

    def to_surface_context(self, *, route: str = "", native_capabilities: Tuple[str, ...] = ()) -> SurfaceContext:
        object_type = "card" if self.card_id else ("dashboard" if self.dashboard_id else "")
        object_ids = tuple(x for x in (self.card_id or self.dashboard_id,) if x)
        return SurfaceContext(
            app_id="metabase", route=route, object_type=object_type, object_ids=object_ids,
            selection=self.selected_entities, filters=dict(self.dashboard_filters),
            view_state={"analytics": {
                "dashboard_id": self.dashboard_id, "card_id": self.card_id,
                "selected_rows": [dict(r) for r in self.selected_rows],
                "selected_chart_region": dict(self.selected_chart_region),
                "source_lineage": list(self.source_lineage)}},
            native_capabilities=native_capabilities)


def analytics_from_surface(ctx: SurfaceContext) -> AnalyticsContext | None:
    """Recover an AnalyticsContext from a SurfaceContext produced by :meth:`AnalyticsContext.to_surface_context`."""
    if ctx.app_id != "metabase":
        return None
    a = dict(ctx.view_state.get("analytics", {}))
    return AnalyticsContext(
        dashboard_id=a.get("dashboard_id", ""), card_id=a.get("card_id", ""),
        dashboard_filters=dict(ctx.filters), selected_entities=tuple(ctx.selection),
        selected_rows=tuple(a.get("selected_rows", ())),
        selected_chart_region=a.get("selected_chart_region", {}) or {},
        source_lineage=tuple(a.get("source_lineage", ())))


__all__ = ["AnalyticsContext", "analytics_from_surface"]
