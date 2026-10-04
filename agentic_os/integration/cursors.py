"""Integration plane — cursor state, lag, and poll reconciliation (§5.2/§5.3).

Webhooks are hints, not truth: providers drop events, have outages, and deliver out of order. So each Resource
keeps a cursor/watermark and a last-sync time (lag is exposed to the Control Tower), and important resources
combine webhook ingestion with a periodic source read to catch what the webhook stream lost.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


def _epoch() -> float:
    return time.time()


@dataclass
class CursorState:
    resource_id: str
    cursor: str = ""                   # opaque watermark (provider page token / max id / timestamp)
    last_sync_at: float = 0.0
    events_seen: int = 0


class CursorStore:
    """Per-Resource cursor + lag (in-memory with a persistence seam)."""

    def __init__(self) -> None:
        self._state: dict[str, CursorState] = {}

    def get(self, resource_id: str) -> CursorState:
        return self._state.setdefault(resource_id, CursorState(resource_id=resource_id))

    def advance(self, resource_id: str, cursor: str, *, at: Optional[float] = None, delta_events: int = 0) -> CursorState:
        st = self.get(resource_id)
        st.cursor = cursor
        st.last_sync_at = at if at is not None else _epoch()
        st.events_seen += delta_events
        return st

    def lag_seconds(self, resource_id: str, *, now: Optional[float] = None) -> float:
        st = self.get(resource_id)
        if not st.last_sync_at:
            return float("inf")            # never synced
        return max(0.0, (now if now is not None else _epoch()) - st.last_sync_at)

    def lagging(self, threshold_s: float, *, now: Optional[float] = None) -> list[str]:
        """Resources whose lag exceeds the threshold — the Control Tower's 'stale connector' list."""
        return [rid for rid in self._state if self.lag_seconds(rid, now=now) > threshold_s]


def detect_missed_events(source_external_ids: set[str], ingested_external_ids: set[str]) -> set[str]:
    """Poll reconciliation (§5.2): object ids present at the SOURCE but never seen by the inbox = lost webhooks.
    These need a backfill — fetch the object and raise the obligation the missed event would have."""
    return set(source_external_ids) - set(ingested_external_ids)
