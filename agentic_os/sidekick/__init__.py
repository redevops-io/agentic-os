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
from .twenty import TwentySurfaceAdapter
from .capability import (
    CapabilityDomain, CapabilityMatch, CapabilityRegistry, Maturity, SidekickCapability,
    default_registry, register_capability,
)
from .catalog import register_builtin_capabilities
from .workers import (
    Claim, ClaimConflict, DEFAULT_POLICIES, MERGE_POLICY_ALIASES, MergePolicy, MergeReceipt, MergedSidekickResult,
    SidekickWorkerContext, SidekickWorkerResult, WorkerResultType, coerce_merge_policy, merge_worker_results,
)

__all__ = [
    "SidekickMode", "SurfaceContext", "SurfaceRef", "ArtifactLink", "SurfaceHandoff",
    "SidekickRequest", "SidekickResponse", "SidekickSurfaceAdapter",
    "SidekickSession", "InMemorySessionStore",
    "AnalyticsContext", "analytics_from_surface",
    "TwentySurfaceAdapter",
    "CapabilityDomain", "Maturity", "SidekickCapability", "CapabilityMatch", "CapabilityRegistry",
    "default_registry", "register_capability", "register_builtin_capabilities",
    "WorkerResultType", "MergePolicy", "Claim", "SidekickWorkerContext", "SidekickWorkerResult",
    "ClaimConflict", "MergeReceipt", "MergedSidekickResult", "DEFAULT_POLICIES", "merge_worker_results",
    "MERGE_POLICY_ALIASES", "coerce_merge_policy",
]
