"""The durable pending-action journal and transactional outbox (SQLite, WAL).

Why a database next to the hash-chained ``EventStore``: after the bridge acknowledges an invocation (HTTP 200) the runtime will not send it
again, so the bridge must be able to finish it after a crash. That needs the arguments, the state and the undelivered result in one atomic
store. The ``EventStore`` remains the audit trail (every transition is appended to it); this database is the system of record for recovery.

State machine of one action row (every transition is a compare-and-set, so two workers or two processes cannot both take a step)::

    accepted -> dispatching -> decided                       (the result is queued in ``deliveries`` in the SAME transaction)
    accepted -> awaiting_approval -> accepted               (a person approved in the approvals inbox; the identical request is run)
    accepted/awaiting_approval -> decided (cancelled)

``dispatching`` is the write boundary: a row found there after a crash may have reached the downstream system, and recovery decides what
became of it from the authority ledger, never from the row.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS actions(
  rec_key TEXT PRIMARY KEY, tenant TEXT NOT NULL, key_id TEXT NOT NULL, principal TEXT NOT NULL, group_id TEXT NOT NULL,
  tool_call_id TEXT NOT NULL, call_id TEXT, operation TEXT NOT NULL, action_id TEXT NOT NULL, args_sha256 TEXT NOT NULL, args_json TEXT,
  callback_url TEXT NOT NULL, user_hash TEXT, state TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  receipt_ref TEXT NOT NULL DEFAULT '', corr TEXT NOT NULL DEFAULT '', approval_id TEXT NOT NULL DEFAULT '',
  side_effecting INTEGER NOT NULL DEFAULT 0, local INTEGER NOT NULL DEFAULT 0, subscription INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, expires_at REAL NOT NULL, decided_at REAL, next_at REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS actions_action ON actions(tenant, action_id);
CREATE INDEX IF NOT EXISTS actions_state ON actions(state, next_at);
CREATE TABLE IF NOT EXISTS deliveries(
  event_id TEXT PRIMARY KEY, rec_key TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, body_json TEXT NOT NULL,
  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL, created_at REAL NOT NULL, delivered_at REAL,
  last_detail TEXT NOT NULL DEFAULT '', UNIQUE(rec_key, seq));
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(state, next_at);
CREATE TABLE IF NOT EXISTS subs(
  rec_key TEXT PRIMARY KEY, tenant TEXT NOT NULL, principal TEXT NOT NULL, group_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
  target_action_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, last_sig TEXT NOT NULL DEFAULT '', next_seq INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS lineage(
  tenant TEXT NOT NULL, key_id TEXT NOT NULL, group_id TEXT NOT NULL, parent_group_id TEXT NOT NULL DEFAULT '', ancestors_json TEXT NOT NULL,
  authority_key TEXT NOT NULL DEFAULT '', child_id TEXT NOT NULL DEFAULT '', depth INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(tenant, key_id, group_id));
"""

ACTION_COLS = ("rec_key tenant key_id principal group_id tool_call_id call_id operation action_id args_sha256 args_json callback_url user_hash "
               "state status reason receipt_ref corr approval_id side_effecting local subscription cancel_requested created_at expires_at "
               "decided_at next_at").split()


def rec_key(tenant: str, group_id: str, tool_call_id: str) -> str:
    return json.dumps([tenant, group_id, tool_call_id], separators=(",", ":"))


class OutboxStore:
    def __init__(self, path: str, clock=time.time):
        self.path, self.clock = path, clock
        self._tl = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            self._tl.c = c
        return c

    @contextmanager
    def tx(self):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    # ── actions ───────────────────────────────────────────────────────────────────────────────────────────────
    def insert_action(self, row: Dict[str, Any], delivery: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[dict]]:
        """Insert a new action (and, for an action that is decided on arrival, its first delivery) atomically.
        Returns ("new"|"duplicate"|"conflict", existing-row-or-None). A duplicate has the same operation and argument hash; anything else is a conflict."""
        with self.tx() as c:
            cur = c.execute(f"INSERT OR IGNORE INTO actions({','.join(ACTION_COLS)}) VALUES({','.join('?' * len(ACTION_COLS))})",
                            [row.get(k) for k in ACTION_COLS])
            if cur.rowcount == 0:
                ex = dict(c.execute("SELECT * FROM actions WHERE rec_key=?", (row["rec_key"],)).fetchone())
                same = ex["operation"] == row["operation"] and ex["args_sha256"] == row["args_sha256"] and ex["key_id"] == row["key_id"]
                return ("duplicate" if same else "conflict"), ex
            if delivery:
                self._put_delivery(c, delivery)
        return "new", None

    @staticmethod
    def _put_delivery(c, d: Dict[str, Any]) -> None:
        c.execute("INSERT OR IGNORE INTO deliveries(event_id,rec_key,seq,kind,body_json,state,attempts,next_at,created_at) VALUES(?,?,?,?,?,'pending',0,?,?)",
                  (d["event_id"], d["rec_key"], d["seq"], d["kind"], d["body_json"], d.get("next_at", 0.0), d.get("created_at", time.time())))

    def get(self, key: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM actions WHERE rec_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def by_action(self, tenant: str, action_id: str, *, exclude: str = "") -> List[dict]:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM actions WHERE tenant=? AND action_id=? AND rec_key<>? ORDER BY created_at", (tenant, action_id, exclude))]

    def newest_by_action(self, tenant: str, action_id: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM actions WHERE tenant=? AND action_id=? AND local=0 ORDER BY created_at DESC LIMIT 1",
                                 (tenant, action_id)).fetchone()
        return dict(r) if r else None

    def cas(self, key: str, from_states: Iterable[str], to_state: str, *, delivery: Optional[Dict[str, Any]] = None, **fields: Any) -> bool:
        """Compare-and-set the state (and set ``fields``) in one transaction, optionally queueing a delivery in the same transaction."""
        fs = tuple(from_states)
        sets = ["state=?"] + [f"{k}=?" for k in fields]
        with self.tx() as c:
            cur = c.execute(f"UPDATE actions SET {','.join(sets)} WHERE rec_key=? AND state IN ({','.join('?' * len(fs))})",
                            [to_state, *fields.values(), key, *fs])
            if cur.rowcount != 1:
                return False
            if delivery:
                self._put_delivery(c, delivery)
        return True

    def update(self, key: str, **fields: Any) -> None:
        with self.tx() as c:
            c.execute(f"UPDATE actions SET {','.join(k + '=?' for k in fields)} WHERE rec_key=?", [*fields.values(), key])

    def in_state(self, state: str, *, due_before: Optional[float] = None, limit: int = 100) -> List[dict]:
        if due_before is None:
            q, a = "SELECT * FROM actions WHERE state=? ORDER BY created_at LIMIT ?", (state, limit)
        else:
            q, a = "SELECT * FROM actions WHERE state=? AND next_at<=? ORDER BY created_at LIMIT ?", (state, due_before, limit)
        return [dict(r) for r in self._conn().execute(q, a)]

    def inflight_count(self, tenant: str, key_id: str) -> int:
        return self._conn().execute(
            "SELECT COUNT(*) FROM actions WHERE tenant=? AND key_id=? AND state IN ('accepted','dispatching','awaiting_approval')",
            (tenant, key_id)).fetchone()[0]

    def thread_actions(self, tenant: str, group_id: str) -> List[dict]:
        return [dict(r) for r in self._conn().execute("SELECT * FROM actions WHERE tenant=? AND group_id=?", (tenant, group_id))]

    # ── deliveries ────────────────────────────────────────────────────────────────────────────────────────────
    def add_delivery(self, d: Dict[str, Any]) -> None:
        with self.tx() as c:
            self._put_delivery(c, d)

    def due_deliveries(self, now: float, limit: int = 100) -> List[dict]:
        """The lowest pending sequence of each action whose delivery is due (messages of one action are delivered in order)."""
        rows = self._conn().execute(
            "SELECT d.* FROM deliveries d WHERE d.state='pending' AND d.next_at<=? AND d.seq=("
            " SELECT MIN(seq) FROM deliveries x WHERE x.rec_key=d.rec_key AND x.state='pending') ORDER BY d.created_at LIMIT ?", (now, limit))
        return [dict(r) for r in rows]

    def delivery(self, event_id: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
        return dict(r) if r else None

    def deliveries_for(self, key: str) -> List[dict]:
        return [dict(r) for r in self._conn().execute("SELECT * FROM deliveries WHERE rec_key=? ORDER BY seq", (key,))]

    def mark_delivery(self, event_id: str, state: str, *, next_at: float = 0.0, detail: str = "", attempts_inc: int = 1) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE deliveries SET state=?, next_at=?, last_detail=?, attempts=attempts+?, delivered_at=? WHERE event_id=? AND state='pending'",
                            (state, next_at, detail[:200], attempts_inc, time.time() if state == "delivered" else None, event_id))
            return cur.rowcount == 1

    def drop_pending(self, key: str) -> int:
        with self.tx() as c:
            return c.execute("UPDATE deliveries SET state='dropped' WHERE rec_key=? AND state='pending'", (key,)).rowcount

    def next_seq(self, key: str) -> int:
        r = self._conn().execute("SELECT COALESCE(MAX(seq),0)+1 FROM deliveries WHERE rec_key=?", (key,)).fetchone()
        return int(r[0])

    def purge_arguments(self, key: str) -> None:
        """Once an action is decided and its result delivered, the arguments are no longer needed for recovery: drop them."""
        with self.tx() as c:
            c.execute("UPDATE actions SET args_json=NULL WHERE rec_key=? AND state='decided' AND args_json IS NOT NULL", (key,))

    # ── subscriptions ─────────────────────────────────────────────────────────────────────────────────────────
    def add_sub(self, s: Dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO subs(rec_key,tenant,principal,group_id,tool_call_id,target_action_id,active,last_sig,next_seq) VALUES(?,?,?,?,?,?,1,?,?)",
                      (s["rec_key"], s["tenant"], s["principal"], s["group_id"], s["tool_call_id"], s["target_action_id"], s.get("last_sig", ""), s.get("next_seq", 1)))

    def sub(self, key: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM subs WHERE rec_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def active_subs(self) -> List[dict]:
        return [dict(r) for r in self._conn().execute("SELECT * FROM subs WHERE active=1")]

    def end_sub(self, key: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE subs SET active=0 WHERE rec_key=? AND active=1", (key,)).rowcount == 1

    def sub_event(self, key: str, sig: str, delivery: Dict[str, Any], *, final: bool) -> bool:
        """Advance a subscription and queue its event atomically, only if it is still active (an event is never queued after a cancel)."""
        with self.tx() as c:
            cur = c.execute("UPDATE subs SET last_sig=?, next_seq=next_seq+1, active=? WHERE rec_key=? AND active=1", (sig, 0 if final else 1, key))
            if cur.rowcount != 1:
                return False
            self._put_delivery(c, delivery)
        return True

    # ── lineage ───────────────────────────────────────────────────────────────────────────────────────────────
    def lineage(self, tenant: str, key_id: str, group_id: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM lineage WHERE tenant=? AND key_id=? AND group_id=?", (tenant, key_id, group_id)).fetchone()
        return dict(r) if r else None

    def put_lineage(self, row: Dict[str, Any]) -> bool:
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO lineage(tenant,key_id,group_id,parent_group_id,ancestors_json,authority_key,child_id,depth,closed) VALUES(?,?,?,?,?,?,?,?,0)",
                            (row["tenant"], row["key_id"], row["group_id"], row["parent_group_id"], row["ancestors_json"], row["authority_key"], row["child_id"], row["depth"]))
            return cur.rowcount == 1

    def update_lineage(self, tenant: str, key_id: str, group_id: str, **fields: Any) -> None:
        with self.tx() as c:
            c.execute(f"UPDATE lineage SET {','.join(k + '=?' for k in fields)} WHERE tenant=? AND key_id=? AND group_id=?",
                      [*fields.values(), tenant, key_id, group_id])

    def lineage_children(self, tenant: str, key_id: str, group_id: str) -> List[dict]:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM lineage WHERE tenant=? AND key_id=? AND ancestors_json LIKE ?", (tenant, key_id, '%' + json.dumps(group_id) + '%'))
            if group_id in json.loads(r["ancestors_json"])]
