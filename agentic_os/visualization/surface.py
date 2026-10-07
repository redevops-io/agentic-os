"""MetabaseSurfaceAdapter — makes Metabase a first-class Sidekick surface.

Implements the portable ``SidekickSurfaceAdapter`` contract for Metabase: it reports what the user is looking
at/selecting as a :class:`SurfaceContext` (carrying an :class:`AnalyticsContext`), builds native deep links and
OSS **signed-embed** links (so the dock can show the real dashboard in-context, L2), and mints navigable
:class:`ArtifactLink`s. This is the bridge between the visualization package and the one-Sidekick session layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Any

from ..sidekick import AnalyticsContext, ArtifactLink, SurfaceContext
from .embed import embed_url


@dataclass
class MetabaseSurfaceAdapter:
    """A Sidekick surface adapter for a Metabase instance + the user's current analytics context."""
    base_url: str
    embedding_secret: str = ""
    analytics: AnalyticsContext = field(default_factory=AnalyticsContext)
    surface_type: str = "metabase"

    def context(self) -> SurfaceContext:
        route = f"/dashboard/{self.analytics.dashboard_id}" if self.analytics.dashboard_id else (
            f"/question/{self.analytics.card_id}" if self.analytics.card_id else "")
        return self.analytics.to_surface_context(
            route=route, native_capabilities=("analytics.question.read", "analytics.dashboard.read",
                                               "analytics.query.execute"))

    def deep_link(self, resource_type: str, resource_id: str) -> str:
        """Native Metabase URL (opens the real app, same session)."""
        kind = "dashboard" if resource_type.startswith("dash") else "question"
        return f"{self.base_url.rstrip('/')}/{kind}/{resource_id}"

    def embed_link(self, resource_type: str, resource_id: int, *, params: Mapping[str, Any] | None = None,
                   ttl_seconds: int = 600) -> str:
        """Signed static-embed URL for an iframe (OSS-compatible). Requires ``embedding_secret``."""
        kind = "dashboard" if resource_type.startswith("dash") else "question"
        return embed_url(self.base_url, kind, int(resource_id), self.embedding_secret,
                         params=params, ttl_seconds=ttl_seconds)

    def artifact_link(self, resource_type: str, resource_id: str, *, project_id: str = "",
                      session_id: str = "") -> ArtifactLink:
        return ArtifactLink(provider="metabase", resource_type=resource_type, resource_id=str(resource_id),
                            native_url=self.deep_link(resource_type, resource_id), project_id=project_id,
                            sidekick_session_id=session_id)


__all__ = ["MetabaseSurfaceAdapter"]
