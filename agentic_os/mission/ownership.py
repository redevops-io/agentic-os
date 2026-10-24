"""Mission ownership: one driver per mission across processes, enforced at the WRITE, not just agreed on.

The authority ledger already guarantees a reservation is dispatched at most once, however many replicas race. It says nothing about who
may append to a mission's event log or continue its execution. Two replicas driving one mission can both write events and diverge.

A lease alone is not enough: a driver that stalls, loses its lease and wakes up still believes it owns the mission and will write. So
every managed mission carries a monotonically increasing FENCING TOKEN, and `FencedEventStore` checks the token INSIDE the append: under
SQLite's write lock it verifies "this process's token is the current, unexpired one" and appends the event in the same critical section.
A stale writer is refused (`FencingError`) and cannot interleave with the new owner.

Limit, stated plainly: this serialises writers that share the lease database (replicas on one host, or a shared filesystem). A log
stored somewhere that cannot take part in that critical section cannot be fenced by a lease; such a deployment needs the log itself to
reject stale writers (for example a conditional append on an owner epoch).
"""
from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional


class FencingError(RuntimeError):
    """The writer does not hold the current lease for this mission. Nothing was written."""


class MissionLease:
    def __init__(self, leases: "MissionLeases", mission_id: str, owner: str, token: int, ttl_s: float, expires_at: float) -> None:
        self.leases, self.mission_id, self.owner, self.token = leases, mission_id, owner, token
        self.ttl_s, self.expires_at = ttl_s, expires_at
        self.lost = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def renew(self) -> bool:
        ok = self.leases.renew(self)
        if not ok:
            self.lost = True
        return ok

    def release(self) -> None:
        self._stop.set()
        self.leases.release(self)

    # `with lease:` keeps it alive (heartbeat) for the duration and releases it afterwards
    def __enter__(self) -> "MissionLease":
        if self.ttl_s > 0 and self.leases.heartbeat:
            self._thread = threading.Thread(target=self._beat, name=f"lease-{self.mission_id}", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False

    def _beat(self) -> None:
        while not self._stop.wait(max(0.05, self.ttl_s / 3.0)):
            if not self.renew():
                return


class MissionLeases:
    def __init__(self, path: str, *, clock=time.time, ttl_s: float = 60.0, heartbeat: bool = True) -> None:
        self.path, self.clock, self.ttl_s, self.heartbeat = path, clock, ttl_s, heartbeat
        self.held: "dict[str, MissionLease]" = {}          # this process's leases (shared by all its threads)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS mission_leases (mission_id TEXT PRIMARY KEY, owner TEXT, token INTEGER NOT NULL, "
                      "expires_at REAL NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def acquire(self, mission_id: str, owner: "str | None" = None, ttl_s: "float | None" = None) -> "MissionLease | None":
        """The lease with a NEW fencing token if the mission is free or its lease expired, else None. Idempotent for the same owner
        while its lease is live (the same token: it already owns it)."""
        owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        ttl = float(self.ttl_s if ttl_s is None else ttl_s)
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            now = self.clock()
            row = c.execute("SELECT owner, token, expires_at FROM mission_leases WHERE mission_id=?", (mission_id,)).fetchone()
            if row is None:
                token = 1
                c.execute("INSERT INTO mission_leases VALUES (?,?,?,?)", (mission_id, owner, token, now + ttl))
            elif row[0] == owner and row[2] > now:
                token = row[1]
                c.execute("UPDATE mission_leases SET expires_at=? WHERE mission_id=?", (now + ttl, mission_id))
            elif row[0] is None or row[2] <= now:
                token = row[1] + 1                               # a takeover always advances the fence
                c.execute("UPDATE mission_leases SET owner=?, token=?, expires_at=? WHERE mission_id=?", (owner, token, now + ttl, mission_id))
            else:
                c.execute("ROLLBACK")
                return None
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()
        lease = MissionLease(self, mission_id, owner, token, ttl, now + ttl)
        with self._lock:
            self.held[mission_id] = lease
        return lease

    def renew(self, lease: MissionLease) -> bool:
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            now = self.clock()
            cur = c.execute("UPDATE mission_leases SET expires_at=? WHERE mission_id=? AND owner=? AND token=? AND expires_at>?",
                            (now + lease.ttl_s, lease.mission_id, lease.owner, lease.token, now))
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()
        if cur.rowcount == 1:
            lease.expires_at = now + lease.ttl_s
            return True
        with self._lock:
            if self.held.get(lease.mission_id) is lease:
                del self.held[lease.mission_id]               # we lost it: stop writing
        return False

    def release(self, lease: MissionLease) -> None:
        with self._lock:
            if self.held.get(lease.mission_id) is lease:
                del self.held[lease.mission_id]
        with self._conn() as c:
            c.execute("UPDATE mission_leases SET owner=NULL, expires_at=0 WHERE mission_id=? AND owner=? AND token=?",
                      (lease.mission_id, lease.owner, lease.token))                   # the token row stays, so the fence only moves forward

    def current(self, mission_id: str) -> "dict | None":
        with self._conn() as c:
            r = c.execute("SELECT owner, token, expires_at FROM mission_leases WHERE mission_id=?", (mission_id,)).fetchone()
        return None if r is None else {"owner": r[0], "token": r[1], "expires_at": r[2]}

    def is_managed(self, mission_id: str) -> bool:
        return self.current(mission_id) is not None

    @contextmanager
    def guard(self, mission_id: str, token: int):
        """A critical section in which `token` is VERIFIED to be the current, unexpired lease for the mission. The caller's write
        happens inside it, so no other writer can slip in between the check and the write."""
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT owner, token, expires_at FROM mission_leases WHERE mission_id=?", (mission_id,)).fetchone()
            if r is None or r[1] != token or r[0] is None or r[2] <= self.clock():
                c.execute("ROLLBACK")
                raise FencingError(f"{mission_id}: this writer no longer holds the lease (token {token}"
                                   + (f", current {r[1]}" if r else "") + "); nothing was written")
            try:
                yield
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        finally:
            c.close()


class FencedEventStore:
    """Wraps a mission event store. A managed mission (one that has ever been leased) can only be appended to by a process that holds
    its CURRENT lease, checked inside the append. Reads, and missions that were never leased, pass straight through (unless `strict`)."""

    def __init__(self, inner, leases: MissionLeases, *, strict: bool = False) -> None:
        self._inner, self._leases, self._strict = inner, leases, strict

    def append(self, type: str, mission_id: str, payload: dict):
        if not mission_id or not (self._strict or self._leases.is_managed(mission_id)):
            return self._inner.append(type, mission_id, payload)
        lease = self._leases.held.get(mission_id)
        if lease is None or lease.lost:
            raise FencingError(f"{mission_id}: this process does not hold the mission lease; nothing was written")
        with self._leases.guard(mission_id, lease.token):
            return self._inner.append(type, mission_id, payload)

    def __getattr__(self, name):
        return getattr(self._inner, name)
