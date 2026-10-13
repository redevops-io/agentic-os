"""Per-visitor simulated write overlay for the public permissions demo.

Anonymous visitors of a shared, persistent grant store must not be able to mutate it. Instead their
add/remove requests are applied to a small in-memory overlay keyed by a session cookie: it is never
persisted, never visible to other visitors, capped in size and expires after a TTL. Decisions/previews
for that visitor evaluate against ``base grants + that session's overlay`` via :meth:`OverlayRegistry.view`,
which returns a read-only store-like object accepted by ``PermissionsPlane`` / ``preview``.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Callable

from agentic_os.permissions import Grant, grant_id

SESSION_TTL_S = 30 * 60
MAX_SESSIONS = 500          # oldest-touched session evicted beyond this
MAX_ADDED = 25              # grants a single session may add
MAX_REMOVED = 50            # base-grant tombstones per session
COOKIE_NAME = "perm_demo_sid"


class _Session:
    __slots__ = ("added", "removed", "touched")

    def __init__(self, now: float):
        self.added: dict[str, Grant] = {}
        self.removed: set[str] = set()
        self.touched = now


class OverlayView:
    """Read-only store facade: base grants with one session's overlay applied."""

    def __init__(self, base, sess: _Session | None):
        self._base, self._sess = base, sess

    @property
    def sealed(self) -> bool:
        return bool(getattr(self._base, "sealed", False))

    def list(self) -> list[Grant]:
        base = self._base.list()
        if self.sealed or self._sess is None:
            return base
        out = [g for g in base if g.id not in self._sess.removed and g.id not in self._sess.added]
        out.extend(self._sess.added.values())
        return out

    def get(self, gid: str) -> Grant | None:
        return next((g for g in self.list() if g.id == gid), None)

    def status(self) -> dict:
        st = dict(self._base.status())
        st["count"] = 0 if st.get("sealed") else len(self.list())
        return st


class OverlayRegistry:
    def __init__(self, base, *, ttl: float = SESSION_TTL_S, max_sessions: int = MAX_SESSIONS,
                 max_added: int = MAX_ADDED, max_removed: int = MAX_REMOVED,
                 clock: Callable[[], float] = time.time):
        self.base, self.ttl, self.max_sessions = base, ttl, max_sessions
        self.max_added, self.max_removed, self._clock = max_added, max_removed, clock
        self._s: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for sid in [k for k, v in self._s.items() if now - v.touched > self.ttl]:
            del self._s[sid]

    def _get(self, sid: str | None, create: bool) -> tuple[str | None, _Session | None]:
        now = self._clock()
        self._expire(now)
        if sid and sid in self._s:
            self._s[sid].touched = now
            return sid, self._s[sid]
        if not create:
            return None, None
        while len(self._s) >= self.max_sessions:
            del self._s[min(self._s, key=lambda k: self._s[k].touched)]
        sid = secrets.token_urlsafe(24)
        self._s[sid] = _Session(now)
        return sid, self._s[sid]

    def view(self, sid: str | None) -> OverlayView:
        with self._lock:
            _, sess = self._get(sid, False)
            return OverlayView(self.base, sess)

    def add(self, sid: str | None, subject_kind, subject, resource_kind, resource_name, **kw) -> tuple[str, Grant]:
        """Simulate an add. Raises ValueError when the per-session cap is hit."""
        with self._lock:
            sid, sess = self._get(sid, True)
            gid = grant_id(subject_kind, subject, resource_kind, resource_name)
            if gid not in sess.added and len(sess.added) >= self.max_added:
                raise ValueError(f"demo session limit reached ({self.max_added} simulated grants)")
            sess.removed.discard(gid)
            g = Grant(id=gid, subject_kind=subject_kind, subject=subject, resource_kind=resource_kind,
                      resource_name=resource_name, actions=kw.get("actions") or ["read"],
                      row_scope=kw.get("row_scope", "all"), row_column=kw.get("row_column", "owner"),
                      row_values=kw.get("row_values") or [], masked_columns=kw.get("masked_columns") or [],
                      created_by="admin", at=self._clock())
            sess.added[gid] = g
            return sid, g

    def remove(self, sid: str | None, gid: str) -> tuple[str, bool]:
        with self._lock:
            sid, sess = self._get(sid, True)
            if gid in sess.added:
                del sess.added[gid]
                return sid, True
            if gid in sess.removed or not any(g.id == gid for g in self.base.list()):
                return sid, False
            if len(sess.removed) >= self.max_removed:
                raise ValueError("demo session limit reached")
            sess.removed.add(gid)
            return sid, True

    def __len__(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._s)
