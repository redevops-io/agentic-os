"""Typed Metabase authoring provider — the full workspace CRUD the audit found missing.

Only a flat 4-method writer (create card/dashboard, add dashcards, get card) existed. This defines the complete
typed surface — update/archive/list + dashboard lifecycle + add-to-EXISTING-dashboard — as a Protocol that both
``HttpMetabaseWriter`` and ``InMemoryMetabaseWriter`` already satisfy. All MUTATIONS stay governed (approval-gated)
via ``governed`` — this module only types the capability; it does not relax the gate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .client import MetabaseWriter


@runtime_checkable
class MetabaseWorkspaceProvider(MetabaseWriter, Protocol):
    """Create (inherited) + update/archive/list + dashboard lifecycle + add-to-existing-dashboard."""
    def update_card(self, card_id: int, changes: Dict[str, Any]) -> Dict[str, Any]: ...
    def archive_card(self, card_id: int) -> bool: ...
    def list_cards(self) -> List[Dict[str, Any]]: ...
    def get_dashboard(self, dashboard_id: int) -> Optional[Dict[str, Any]]: ...
    def update_dashboard(self, dashboard_id: int, changes: Dict[str, Any]) -> Dict[str, Any]: ...
    def archive_dashboard(self, dashboard_id: int) -> bool: ...
    def add_card_to_dashboard(self, dashboard_id: int, card_id: int, *,
                              size_x: int = 12, size_y: int = 4) -> bool: ...


__all__ = ["MetabaseWorkspaceProvider"]
