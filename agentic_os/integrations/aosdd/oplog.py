"""Durable operation ledger for the AOS-DD adapters (SQLite, WAL, one writer at a time).

Why a table next to the mission's hash-chained event log: the event log is append-only and cannot say "exactly one process may
perform this operation". This table can: ``op_id`` is the PRIMARY KEY, a claim is one ``BEGIN IMMEDIATE`` transaction, and a
claim carries a fencing counter so a stale executor cannot record a result over a newer one. Every state change is also
appended to ``opevents`` (history). The mission event log receives a copy of each transition (written by the caller) so a
finished mission can be rebuilt from that log alone.

States:  planned -> inflight -> applied | unknown | planned (a definite refusal)
         inflight with an expired lease behaves as unknown (the previous executor may have sent the request)
         applied -> compensating -> compensated | comp_failed
``claim`` is persisted BEFORE the first byte of the request is sent."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

APPLIED, PLANNED, INFLIGHT, UNKNOWN = "applied", "planned", "inflight", "unknown"
COMPENSATING, COMPENSATED, COMP_FAILED = "compensating", "compensated", "comp_failed"


class OpError(RuntimeError):
    pass


class OpConflict(OpError):
    """The same op_id was planned with a different mission/step/target (an id collision or a tampered plan)."""


class FencingError(OpError):
    """A result was offered by an executor that no longer holds the claim."""


@dataclass
class Claim:
    status: str               # acquired | applied | held | reconcile | compensated
    op_id: str
    fence: int = 0
    owner: str = ""
    row: "dict | None" = None


def new_owner() -> str:
    return "exec-" + uuid.uuid4().hex[:12]


class OpLog:
    def __init__(self, path: str, lease_ttl_s: float = 120.0, clock=time.time):
        self.path, self.lease_ttl_s, self.clock = path, lease_ttl_s, clock
        self._local = threading.local()
        with self._tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS ops (op_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, step TEXT NOT NULL, app TEXT NOT NULL, "
                      "capability TEXT NOT NULL, target_digest TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, owner TEXT, "
                      "fence INTEGER NOT NULL DEFAULT 0, lease_until REAL, native_id TEXT, result TEXT, error TEXT, "
                      "created REAL NOT NULL, updated REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS opevents (seq INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT NOT NULL, ts REAL NOT NULL, "
                      "state TEXT NOT NULL, note TEXT)")

    # ---- plumbing -----------------------------------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.row_factory = sqlite3.Row
            self._local.c = c
        return c

    class _Tx:
        def __init__(self, c):
            self.c = c

        def __enter__(self):
            self.c.execute("BEGIN IMMEDIATE")
            return self.c

        def __exit__(self, et, ev, tb):
            self.c.execute("ROLLBACK" if et else "COMMIT")
            return False

    def _tx(self):
        return OpLog._Tx(self._conn())

    def _hist(self, c, op: str, state: str, note: str = "") -> None:
        c.execute("INSERT INTO opevents (op_id, ts, state, note) VALUES (?,?,?,?)", (op, self.clock(), state, note[:300]))

    @staticmethod
    def _d(row) -> "dict | None":
        if row is None:
            return None
        d = dict(row)
        for k in ("result",):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    # ---- reads ---------------------------------------------------------------------------------------------------
    def get(self, op: str) -> "dict | None":
        return self._d(self._conn().execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone())

    def for_mission(self, mission_id: str) -> list:
        return [self._d(r) for r in self._conn().execute("SELECT * FROM ops WHERE mission_id=? ORDER BY created, op_id", (mission_id,))]

    def history(self, op: str) -> list:
        return [dict(r) for r in self._conn().execute("SELECT ts, state, note FROM opevents WHERE op_id=? ORDER BY seq", (op,))]

    # ---- transitions ------------------------------------------------------------------------------------------------
    def plan_op(self, op: str, mission_id: str, step: str, app: str, capability: str, target_digest: str, attempt: int = 1) -> dict:
        with self._tx() as c:
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is not None:
                if (row["mission_id"], row["step"], row["target_digest"], row["capability"]) != (mission_id, step, target_digest, capability):
                    raise OpConflict(f"{op} already exists for a different mission/step/target")
                return self._d(row)
            now = self.clock()
            c.execute("INSERT INTO ops (op_id, mission_id, step, app, capability, target_digest, attempt, state, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (op, mission_id, step, app, capability, target_digest, int(attempt), PLANNED, now, now))
            self._hist(c, op, PLANNED)
            return self._d(c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone())

    def claim(self, op: str, owner: str) -> Claim:
        """Take the right to send the request for ``op`` (persisted before any request), or learn why not."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is None:
                raise OpError(f"{op} was never planned")
            now, st = self.clock(), row["state"]
            if st == APPLIED:
                return Claim("applied", op, row["fence"], row["owner"] or "", self._d(row))
            if st in (COMPENSATING, COMPENSATED, COMP_FAILED):
                return Claim("compensated", op, row["fence"], row["owner"] or "", self._d(row))
            if st == UNKNOWN or (st == INFLIGHT and (row["lease_until"] or 0) <= now):
                return Claim("reconcile", op, row["fence"], row["owner"] or "", self._d(row))
            if st == INFLIGHT:
                if row["owner"] == owner:                     # the same executor re-entering its own claim
                    return Claim("acquired", op, row["fence"], owner, self._d(row))
                return Claim("held", op, row["fence"], row["owner"] or "", self._d(row))
            fence = row["fence"] + 1
            c.execute("UPDATE ops SET state=?, owner=?, fence=?, lease_until=?, updated=? WHERE op_id=?",
                      (INFLIGHT, owner, fence, now + self.lease_ttl_s, now, op))
            self._hist(c, op, INFLIGHT, f"owner={owner} fence={fence}")
            return Claim("acquired", op, fence, owner, self._d(c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()))

    def _fenced(self, c, op: str, fence: int) -> sqlite3.Row:
        row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
        if row is None or row["fence"] != fence or row["state"] != INFLIGHT:
            raise FencingError(f"{op}: the claim (fence {fence}) is no longer held")
        return row

    def record_applied(self, op: str, fence: int, native_id: str, result: dict, note: str = "") -> None:
        with self._tx() as c:
            self._fenced(c, op, fence)
            c.execute("UPDATE ops SET state=?, native_id=?, result=?, error=NULL, lease_until=NULL, updated=? WHERE op_id=?",
                      (APPLIED, str(native_id), json.dumps(result, default=str), self.clock(), op))
            self._hist(c, op, APPLIED, note or str(native_id))

    def record_unknown(self, op: str, fence: int, error: str) -> None:
        with self._tx() as c:
            self._fenced(c, op, fence)
            c.execute("UPDATE ops SET state=?, error=?, updated=? WHERE op_id=?", (UNKNOWN, error[:500], self.clock(), op))
            self._hist(c, op, UNKNOWN, error)

    def record_refused(self, op: str, fence: int, error: str) -> None:
        """A definite refusal by the application (the request did not take effect): retryable under the same op id."""
        with self._tx() as c:
            self._fenced(c, op, fence)
            c.execute("UPDATE ops SET state=?, error=?, lease_until=NULL, updated=? WHERE op_id=?", (PLANNED, error[:500], self.clock(), op))
            self._hist(c, op, PLANNED, "refused: " + error)

    def resolve_applied(self, op: str, native_id: str, result: dict, note: str) -> None:
        """Reconciliation FOUND the record in the application (by marker): adopt it. No second write."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is None or row["state"] not in (UNKNOWN, INFLIGHT, PLANNED):
                raise OpError(f"{op}: nothing to resolve")
            c.execute("UPDATE ops SET state=?, native_id=?, result=?, error=NULL, lease_until=NULL, fence=fence+1, updated=? WHERE op_id=?",
                      (APPLIED, str(native_id), json.dumps(result, default=str), self.clock(), op))
            self._hist(c, op, APPLIED, "adopted: " + note)

    def resolve_not_applied(self, op: str, note: str) -> None:
        """Reconciliation found NOTHING in the application: the op may be claimed again (fence advances, stale executors are fenced off)."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is None or row["state"] not in (UNKNOWN, INFLIGHT):
                raise OpError(f"{op}: nothing to resolve")
            c.execute("UPDATE ops SET state=?, owner=NULL, lease_until=NULL, fence=fence+1, updated=? WHERE op_id=?", (PLANNED, self.clock(), op))
            self._hist(c, op, PLANNED, "not applied: " + note)

    def begin_compensation(self, op: str) -> bool:
        with self._tx() as c:
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is None or row["state"] not in (APPLIED, COMP_FAILED):
                return False
            c.execute("UPDATE ops SET state=?, updated=? WHERE op_id=?", (COMPENSATING, self.clock(), op))
            self._hist(c, op, COMPENSATING)
            return True

    def end_compensation(self, op: str, ok: bool, note: str = "") -> None:
        with self._tx() as c:
            c.execute("UPDATE ops SET state=?, error=?, updated=? WHERE op_id=?", (COMPENSATED if ok else COMP_FAILED, None if ok else note[:500], self.clock(), op))
            self._hist(c, op, COMPENSATED if ok else COMP_FAILED, note)
