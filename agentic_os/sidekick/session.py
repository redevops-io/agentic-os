"""The Sidekick session — one durable conversation/context that travels across surfaces.

Opening another application does NOT start a new conversation; the session follows the user, carrying project,
evidence, artifacts, active missions and authority (§38/§57). ``InMemorySessionStore`` is the reference backbone
(an enterprise overlay persists it); ``switch_surface`` records an explicit :class:`SurfaceHandoff` and preserves
project + conversation context, which is the whole point.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contracts import ArtifactLink, SurfaceHandoff, SurfaceRef


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class SidekickSession:
    """Durable session state (§57). Mutable because a session evolves — but every mutation goes through the store
    so handoffs are recorded. ``conversation`` / ``authority_context`` / ``principal`` are kept opaque here; the
    enterprise overlay binds them to real Principal + inbox + ledger."""
    session_id: str
    tenant_id: str = ""
    project_id: str = ""
    principal: Any = None
    current_surface: Optional[SurfaceRef] = None
    recent_surfaces: List[SurfaceRef] = field(default_factory=list)
    conversation: List[Dict[str, Any]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    artifacts: List[ArtifactLink] = field(default_factory=list)
    active_missions: List[str] = field(default_factory=list)
    authority_context: Any = None
    created_at: int = field(default_factory=_now_ms)

    def view(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "tenant_id": self.tenant_id, "project_id": self.project_id,
                "current_surface": self.current_surface.app_id if self.current_surface else None,
                "recent_surfaces": [s.app_id for s in self.recent_surfaces],
                "artifacts": len(self.artifacts), "active_missions": list(self.active_missions),
                "messages": len(self.conversation)}


@dataclass
class InMemorySessionStore:
    """Reference session service: create / get / move-between-surfaces / attach artifacts + missions."""
    _sessions: Dict[str, SidekickSession] = field(default_factory=dict)

    def create(self, *, tenant_id: str = "", project_id: str = "", principal: Any = None,
               surface: Optional[SurfaceRef] = None) -> SidekickSession:
        s = SidekickSession(session_id="sk_" + secrets.token_urlsafe(10), tenant_id=tenant_id,
                            project_id=project_id, principal=principal, current_surface=surface)
        if surface is not None:
            s.recent_surfaces.append(surface)
        self._sessions[s.session_id] = s
        return s

    def get(self, session_id: str) -> Optional[SidekickSession]:
        return self._sessions.get(session_id)

    def switch_surface(self, session_id: str, to: SurfaceRef, *, reason: str = "",
                       artifact: Optional[ArtifactLink] = None) -> SurfaceHandoff:
        """Move the SAME session to another surface, preserving project + conversation. Returns the handoff
        event. Raises KeyError for an unknown session (never silently forks a new one)."""
        s = self._sessions[session_id]
        frm = s.current_surface
        s.current_surface = to
        s.recent_surfaces.append(to)
        return SurfaceHandoff(sidekick_session_id=session_id, project_id=s.project_id,
                              from_surface=frm, to_surface=to, reason=reason, artifact=artifact)

    def add_message(self, session_id: str, role: str, text: str) -> None:
        self._sessions[session_id].conversation.append({"role": role, "text": text, "at": _now_ms()})

    def add_artifact(self, session_id: str, artifact: ArtifactLink) -> None:
        self._sessions[session_id].artifacts.append(artifact)

    def attach_mission(self, session_id: str, mission_id: str) -> None:
        s = self._sessions[session_id]
        if mission_id not in s.active_missions:
            s.active_missions.append(mission_id)


__all__ = ["SidekickSession", "InMemorySessionStore"]
