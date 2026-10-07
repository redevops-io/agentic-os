"""Portable Sidekick layer — one session/context model rendered across many specialist surfaces.

"One Sidekick. Many specialist surfaces. One Runtime underneath." This package is the backbone the plan's
Phase B/C call for: a durable :class:`SidekickSession` that travels with the user, a typed
:class:`SurfaceContext` each app reports (so follow-ups carry context), explicit :class:`SurfaceHandoff`s between
surfaces, the :class:`SidekickRequest`/:class:`SidekickResponse` interaction contract, and
:class:`AnalyticsContext` for Metabase. Interaction contract only — distinct from the standalone coding-agent
"sidekick" repo. The enterprise overlay renders it as a dock inside each app and binds it to the real
Principal/inbox/Mission Runtime.
"""
from .contracts import (
    ArtifactLink, SidekickMode, SidekickRequest, SidekickResponse, SidekickSurfaceAdapter, SurfaceContext,
    SurfaceHandoff, SurfaceRef,
)
from .session import InMemorySessionStore, SidekickSession
from .analytics import AnalyticsContext, analytics_from_surface

__all__ = [
    "SidekickMode", "SurfaceContext", "SurfaceRef", "ArtifactLink", "SurfaceHandoff",
    "SidekickRequest", "SidekickResponse", "SidekickSurfaceAdapter",
    "SidekickSession", "InMemorySessionStore",
    "AnalyticsContext", "analytics_from_surface",
]
