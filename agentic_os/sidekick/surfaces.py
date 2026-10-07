"""More Sidekick surface adapters — Chatwoot, ERPNext, Postiz (plan §23/§25, P9).

The audit found only Twenty + Metabase surface adapters built (Chatwoot/ERPNext/Postiz named in docstrings only).
These complete the priority surface set so one SidekickSession travels across the specialist apps the commercial
lifecycle actually uses. Each follows the same shape as TwentySurfaceAdapter: report what the user is on as a typed
SurfaceContext + provide native deep links. Pure, provider-neutral, open-core (the enterprise overlay renders them
as docks and binds real auth).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Tuple

from .contracts import ArtifactLink, SurfaceContext, SurfaceRef


@dataclass
class ChatwootSurfaceAdapter:
    """Customer-conversation workspace. The user is on a conversation/contact; follow-ups carry that context."""
    base_url: str
    account_id: str = ""
    object_type: str = "conversation"       # "conversation" | "contact"
    object_id: str = ""
    selection: Tuple[str, ...] = ()
    surface_type: str = "chatwoot"

    def context(self) -> SurfaceContext:
        seg = "conversations" if self.object_type == "conversation" else "contacts"
        route = f"/app/accounts/{self.account_id}/{seg}/{self.object_id}" if self.object_id else \
                f"/app/accounts/{self.account_id}/{seg}"
        return SurfaceContext(app_id="chatwoot", route=route, object_type=self.object_type,
                              object_ids=(self.object_id,) if self.object_id else (), selection=self.selection,
                              native_capabilities=("support.conversation.read", "support.conversation.reply",
                                                   "support.contact.read"))

    def deep_link(self, resource_type: str, resource_id: str) -> str:
        seg = "conversations" if resource_type == "conversation" else "contacts"
        return f"{self.base_url.rstrip('/')}/app/accounts/{self.account_id}/{seg}/{resource_id}"

    def artifact_link(self, resource_type: str, resource_id: str, *, project_id: str = "",
                      session_id: str = "") -> ArtifactLink:
        return ArtifactLink(provider="chatwoot", resource_type=resource_type, resource_id=str(resource_id),
                            native_url=self.deep_link(resource_type, resource_id), project_id=project_id,
                            sidekick_session_id=session_id)


@dataclass
class ERPNextSurfaceAdapter:
    """ERP workspace — orders/quotations/invoices/customers. Routes use the Frappe /app/<doctype>/<name> form."""
    base_url: str
    object_type: str = ""                   # "sales_order" | "quotation" | "sales_invoice" | "customer"
    object_id: str = ""
    selection: Tuple[str, ...] = ()
    surface_type: str = "erpnext"

    def _doctype(self, object_type: str) -> str:
        return (object_type or "").replace("_", "-")

    def context(self) -> SurfaceContext:
        dt = self._doctype(self.object_type)
        route = f"/app/{dt}/{self.object_id}" if self.object_id else (f"/app/{dt}" if dt else "")
        return SurfaceContext(app_id="erpnext", route=route, object_type=self.object_type,
                              object_ids=(self.object_id,) if self.object_id else (), selection=self.selection,
                              native_capabilities=("erp.order.read", "erp.quotation.read", "erp.invoice.read",
                                                   "erp.customer.read"))

    def deep_link(self, resource_type: str, resource_id: str) -> str:
        return f"{self.base_url.rstrip('/')}/app/{self._doctype(resource_type)}/{resource_id}"

    def artifact_link(self, resource_type: str, resource_id: str, *, project_id: str = "",
                      session_id: str = "") -> ArtifactLink:
        return ArtifactLink(provider="erpnext", resource_type=resource_type, resource_id=str(resource_id),
                            native_url=self.deep_link(resource_type, resource_id), project_id=project_id,
                            sidekick_session_id=session_id)


@dataclass
class PostizSurfaceAdapter:
    """Content calendar / social publishing workspace."""
    base_url: str
    object_type: str = "calendar"           # "calendar" | "post"
    object_id: str = ""
    selection: Tuple[str, ...] = ()
    surface_type: str = "postiz"

    def context(self) -> SurfaceContext:
        route = f"/launches/{self.object_id}" if (self.object_type == "post" and self.object_id) else "/"
        return SurfaceContext(app_id="postiz", route=route, object_type=self.object_type,
                              object_ids=(self.object_id,) if self.object_id else (), selection=self.selection,
                              native_capabilities=("content.schedule", "content.publish", "content.calendar.read"))

    def deep_link(self, resource_type: str, resource_id: str) -> str:
        if resource_type == "post":
            return f"{self.base_url.rstrip('/')}/launches/{resource_id}"
        return f"{self.base_url.rstrip('/')}/"

    def artifact_link(self, resource_type: str, resource_id: str, *, project_id: str = "",
                      session_id: str = "") -> ArtifactLink:
        return ArtifactLink(provider="postiz", resource_type=resource_type, resource_id=str(resource_id),
                            native_url=self.deep_link(resource_type, resource_id), project_id=project_id,
                            sidekick_session_id=session_id)


class SessionStore(Protocol):
    """The session-store seam (§38). ``InMemorySessionStore`` is the reference backbone; the enterprise overlay
    provides a DURABLE implementation (the audit flagged in-memory-only) against this interface."""
    def create(self, *, tenant_id: str = ..., project_id: str = ..., principal: Any = ...,
               surface: SurfaceRef = ...): ...
    def get(self, session_id: str): ...
    def switch_surface(self, session_id: str, to: SurfaceRef, *, reason: str = ...): ...
    def add_message(self, session_id: str, role: str, text: str) -> None: ...
    def add_artifact(self, session_id: str, artifact: ArtifactLink) -> None: ...
    def attach_mission(self, session_id: str, mission_id: str) -> None: ...


__all__ = ["ChatwootSurfaceAdapter", "ERPNextSurfaceAdapter", "PostizSurfaceAdapter", "SessionStore"]
