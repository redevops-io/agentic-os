"""Compile a :class:`VizSpec` into the exact Metabase REST payloads.

Mirrors the shapes Metabase's API expects (and that ``apps/control-tower/seed.py`` already uses): a native-SQL
``dataset_query`` on ``POST /api/card``, a ``POST /api/dashboard``, and dashcard placement. Pure/deterministic —
no network — so it is fully unit-testable and the governed layer (``governed``) just hands these to a writer.
"""
from __future__ import annotations

from typing import Any, Dict, Sequence

from .contracts import VizSpec


def card_payload(spec: VizSpec) -> Dict[str, Any]:
    """The body for ``POST /api/card`` — a saved question running ``spec.sql`` as native SQL on ``database_id``."""
    payload: Dict[str, Any] = {
        "name": spec.title,
        "display": spec.display.value,
        "visualization_settings": dict(spec.visualization_settings),
        "dataset_query": {
            "type": "native",
            "native": {"query": spec.sql},
            "database": spec.database_id,
        },
    }
    if spec.description:
        payload["description"] = spec.description
    return payload


def dashboard_payload(name: str) -> Dict[str, Any]:
    """The body for ``POST /api/dashboard``."""
    return {"name": name}


def dashcards_payload(card_ids: Sequence[int]) -> list[Dict[str, Any]]:
    """Two-column grid placement for ``PUT /api/dashboard/{id}`` (same layout as the seed script)."""
    return [{"id": -(i + 1), "card_id": cid, "row": (i // 2) * 4, "col": (i % 2) * 12,
             "size_x": 12, "size_y": 4} for i, cid in enumerate(card_ids)]


__all__ = ["card_payload", "dashboard_payload", "dashcards_payload"]
