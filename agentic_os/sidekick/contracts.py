"""Portable Sidekick interaction contracts — "one Sidekick, many surfaces".

These types let a SINGLE Sidekick conversation follow the user across specialist app surfaces (Metabase, Twenty,
Chatwoot, ERPNext, …) instead of each app having its own detached chatbot. A surface adapter reports what the
user is looking at/selecting as a :class:`SurfaceContext`; the Sidekick service receives
``SidekickSession + SurfaceContext + message`` (not a bare chat string) and replies with a
:class:`SidekickResponse` whose proposed changes and candidate actions still flow through Runtime authority.

This is the INTERACTION contract only — deliberately separate from the standalone coding-agent "sidekick" repo
(§18 of the plan). Pure dataclasses, no I/O, provider-neutral.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, Tuple


def _now_ms() -> int:
    return int(time.time() * 1000)


class SidekickMode(str, Enum):
    """What the user is asking Sidekick to do (maps to the §15 action classes)."""
    UNDERSTAND = "understand"
    TRANSFORM = "transform"
    CREATE = "create"
    INVESTIGATE = "investigate"
    ACT = "act"                      # crosses into Mission Runtime — always governed


@dataclass(frozen=True)
class SurfaceContext:
    """What the user is looking at / has selected in a specific app surface (§40). Produced by that app's
    SidekickSurfaceAdapter and sent with every message so follow-ups ("why is this late?", "do the same for the
    other five") carry typed context instead of the user re-explaining."""
    app_id: str                                  # "metabase" | "twenty" | "chatwoot" | "erpnext" | "projects" | ...
    route: str = ""                              # the app-native route/URL path the user is on
    object_type: str = ""                        # e.g. "dashboard" | "opportunity" | "conversation" | "sales_invoice"
    object_ids: Tuple[str, ...] = ()             # the primary object(s) in view
    selection: Tuple[str, ...] = ()              # rows/records/messages the user selected
    filters: Mapping[str, Any] = field(default_factory=dict)
    view_state: Mapping[str, Any] = field(default_factory=dict)   # surface-specific extras (e.g. AnalyticsContext)
    native_capabilities: Tuple[str, ...] = ()    # what native actions the surface offers
    captured_at: int = field(default_factory=_now_ms)


@dataclass(frozen=True)
class SurfaceRef:
    """A compact pointer to a surface the session is (or was) on."""
    app_id: str
    route: str = ""
    object_type: str = ""
    object_id: str = ""


@dataclass(frozen=True)
class ArtifactLink:
    """A typed, navigable reference to something Sidekick created/found, openable in its native UI while keeping
    the same session (§42)."""
    provider: str                                # "metabase" | "twenty" | ...
    resource_type: str                           # "dashboard" | "question" | "opportunity" | ...
    resource_id: str
    native_url: str = ""
    project_id: str = ""
    sidekick_session_id: str = ""


@dataclass(frozen=True)
class SurfaceHandoff:
    """An explicit move of the SAME session from one surface to another (§43), preserving project/evidence."""
    sidekick_session_id: str
    project_id: str
    from_surface: SurfaceRef | None
    to_surface: SurfaceRef
    reason: str = ""
    artifact: ArtifactLink | None = None
    timestamp: int = field(default_factory=_now_ms)


@dataclass(frozen=True)
class SidekickRequest:
    """What the Sidekick service receives — session + current surface + message, never a bare string (§18)."""
    session_id: str
    user_message: str
    surface_context: SurfaceContext | None = None
    selected_objects: Tuple[str, ...] = ()
    attachments: Tuple[str, ...] = ()
    requested_mode: SidekickMode | None = None


@dataclass(frozen=True)
class SidekickResponse:
    """What Sidekick returns — an answer plus governed, inspectable proposals (§18). ``proposed_changes`` and
    ``candidate_actions`` are proposals; ``required_approvals`` lists the gates they must pass before any
    side effect."""
    answer: str = ""
    evidence: Tuple[str, ...] = ()
    proposed_changes: Tuple[Mapping[str, Any], ...] = ()
    candidate_actions: Tuple[Mapping[str, Any], ...] = ()
    required_approvals: Tuple[str, ...] = ()
    artifacts: Tuple[ArtifactLink, ...] = ()


class SidekickSurfaceAdapter(Protocol):
    """Each integrated app implements this so the common Sidekick shell gets typed context + a deep link,
    regardless of how different the host UIs are (§39)."""
    surface_type: str
    def context(self) -> SurfaceContext: ...
    def deep_link(self, resource_type: str, resource_id: str) -> str: ...


__all__ = [
    "SidekickMode", "SurfaceContext", "SurfaceRef", "ArtifactLink", "SurfaceHandoff",
    "SidekickRequest", "SidekickResponse", "SidekickSurfaceAdapter",
]
