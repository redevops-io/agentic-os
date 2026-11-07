"""Durable operation ledger for the AOS-DD adapters (SQLite, WAL, one writer at a time), hash-chained and anchored.

Why a table next to the mission's hash-chained event log: the event log is append-only and cannot say "exactly one process may *attempt*
this operation". This table can: ``op_id`` is the PRIMARY KEY, a claim is one ``BEGIN IMMEDIATE`` transaction, and every claim carries a
GENERATION (a fencing token that increases on EVERY claim, including a re-entry by the same executor and a compensation). Everything that
records a result, and every write, checks the generation the caller holds: a stale holder is refused and its late result is kept as
``stale-generation`` evidence, never applied.

This is NOT exactly-once external execution. It is: at most one executor holds the claim at a time; the claim is persisted before the
request; a request whose outcome is unknown is ``unknown`` and is never retried blindly (``claim`` answers ``reconcile`` until a lookup of
the application by marker/derived id resolves it); a stale holder's late reply is evidence, not a result. A request still in flight when its
lease expires can still reach an application that does not de-duplicate (ERPNext, Chatwoot): that is detected afterwards (two markers),
never prevented.

States:  planned -> inflight -> applied | unknown | planned (a definite refusal)
         inflight with an expired lease behaves as unknown (the previous executor may have sent the request)
         applied -> compensating -> compensated | comp_failed

Integrity. Every state change appends a record to a hash-chained ``oplog_chain`` IN THE SAME TRANSACTION (prev, hash over
``prev|seq|kind|op_id|body``), and the head is kept in a separate anchor file (``<db>.chain``, ``mission.integrity.AnchorFile``, optional
HMAC with ``AGENTIC_OS_ANCHOR_KEY``). The whole chain, the anchor and every state row (against the last chain record about it) are verified
when the ledger is opened; ``claim`` and ``begin_compensation`` run an incremental check first (verified tail unchanged, new records
chain, anchor not ahead of the chain, this operation's row equals its last record). Any failure raises ``OpLogIntegrityError`` and nothing
is claimed. NOT protected: a writer who can rewrite the database AND the anchor consistently (no key configured by default); lease expiry
times are in the records but wall-clock itself is trusted; a database with no anchor yet is trusted on first use."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from agentic_os.mission.integrity import GENESIS, AnchorFile, IntegrityError, anchor_key_from_env

APPLIED, PLANNED, INFLIGHT, UNKNOWN = "applied", "planned", "inflight", "unknown"
COMPENSATING, COMPENSATED, COMP_FAILED = "compensating", "compensated", "comp_failed"
STALE = "stale-generation"


class OpError(RuntimeError):
    pass


class OpConflict(OpError):
    """The same op_id was planned with a different mission/step/target (an id collision or a tampered plan)."""


class FencingError(OpError):
    """The caller does not hold the current generation of the claim (it was superseded, expired and taken over, or resolved)."""


class OpLogIntegrityError(IntegrityError):
    """The operation ledger disagrees with its own hash chain, its anchor, or its state rows. Nothing is claimed."""


@dataclass
class Claim:
    status: str               # acquired | applied | held | reconcile | compensated
    op_id: str
    fence: int = 0
    owner: str = ""
    row: "dict | None" = None


def new_owner() -> str:
    return "exec-" + uuid.uuid4().hex[:12]


def _canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(o) -> str:
    return hashlib.sha256(_canon(o).encode()).hexdigest()


def _rec_hash(prev: str, seq: int, kind: str, op: str, body: str) -> str:
    return "sha256:" + hashlib.sha256(f"{prev}|{seq}|{kind}|{op}|{body}".encode("utf-8")).hexdigest()


class OpLog:
    def __init__(self, path: str, lease_ttl_s: float = 120.0, clock=time.time, *, verify: bool = True, anchor: bool = True, anchor_key: "bytes | None" = None):
        self.path, self.lease_ttl_s, self.clock = path, lease_ttl_s, clock
        self._local = threading.local()
        self._anchor = AnchorFile(path + ".chain", anchor_key if anchor_key is not None else anchor_key_from_env()) if anchor else None
        self._seen: "tuple[int, str] | None" = None
        self._tlock = threading.Lock()
        with self._tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS ops (op_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, step TEXT NOT NULL, app TEXT NOT NULL, "
                      "capability TEXT NOT NULL, target_digest TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, owner TEXT, "
                      "fence INTEGER NOT NULL DEFAULT 0, lease_until REAL, native_id TEXT, result TEXT, error TEXT, "
                      "created REAL NOT NULL, updated REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS cases (case_key TEXT NOT NULL, attempt INTEGER NOT NULL, mission_id TEXT, created REAL NOT NULL, "
                      "PRIMARY KEY (case_key, attempt))")
            c.execute("CREATE TABLE IF NOT EXISTS opevents (seq INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT NOT NULL, ts REAL NOT NULL, "
                      "state TEXT NOT NULL, note TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS oplog_chain (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, op_id TEXT NOT NULL, body TEXT NOT NULL, "
                      "prev TEXT NOT NULL, hash TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS oplog_chain_op ON oplog_chain (op_id, seq)")
        if verify:
            self.verify()

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
        def __init__(self, log):
            self.log, self.c, self.last = log, log._conn(), None

        def __enter__(self):
            self.c.execute("BEGIN IMMEDIATE")
            self.log._local.last = None
            return self.c

        def __exit__(self, et, ev, tb):
            self.c.execute("ROLLBACK" if et else "COMMIT")
            if not et and getattr(self.log._local, "last", None):
                self.log._publish(*self.log._local.last)
            return False

    def _tx(self):
        return OpLog._Tx(self)

    def _hist(self, c, op: str, state: str, note: str = "") -> None:
        c.execute("INSERT INTO opevents (op_id, ts, state, note) VALUES (?,?,?,?)", (op, self.clock(), state, note[:300]))

    # ---- chain --------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _body_of(row) -> dict:
        """What the chain vouches for about an operation row (not the timestamps of creation/update)."""
        return {"state": row["state"], "fence": row["fence"], "owner": row["owner"], "lease_until": row["lease_until"], "native_id": row["native_id"],
                "result_sha256": (hashlib.sha256(row["result"].encode()).hexdigest() if row["result"] else None), "error": row["error"],
                "mission_id": row["mission_id"], "step": row["step"], "capability": row["capability"], "target_digest": row["target_digest"]}

    def _append(self, c, kind: str, op: str, body: dict) -> None:
        last = c.execute("SELECT seq, hash FROM oplog_chain ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS)
        b = _canon(body)
        h = _rec_hash(prev, seq, kind, op, b)
        c.execute("INSERT INTO oplog_chain (seq, kind, op_id, body, prev, hash) VALUES (?,?,?,?,?,?)", (seq, kind, op, b, prev, h))
        self._local.last = (seq, h)

    def _log_op(self, c, kind: str, op: str) -> None:
        self._append(c, kind, op, self._body_of(c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()))

    def _publish(self, seq: int, h: str) -> None:
        with self._tlock:
            if self._seen is None or self._seen[0] < seq:
                self._seen = (seq, h)
        if self._anchor is not None:
            try:
                self._anchor.write({"count": seq, "last_seq": seq, "head": h, "updated": time.time()}, fsync=True)
            except OSError:
                pass          # the chain itself is intact; a missing anchor only weakens truncation detection

    def _anchor_rec(self) -> "dict | None":
        if self._anchor is None:
            return None
        try:
            return self._anchor.read()                         # read BEFORE the chain: a writer commits first and anchors second
        except IntegrityError as e:
            raise OpLogIntegrityError(str(e)) from None

    def _check_anchor(self, c, n: int, rec: "dict | None") -> None:
        if rec is None:
            return
        k = rec["count"]
        if n < k:
            raise OpLogIntegrityError(f"{self.path}: the operation ledger was truncated: it has {n} record(s) and its anchor vouches for {k}")
        if k:
            row = c.execute("SELECT hash FROM oplog_chain WHERE seq=?", (k,)).fetchone()
            if row is None or row["hash"] != rec["head"]:
                raise OpLogIntegrityError(f"{self.path}: the operation ledger disagrees with its anchor at record {k}: rewritten")

    def verify(self) -> dict:
        """Whole-ledger check: chain, anchor, and every state row against the last chain record about it. Raises OpLogIntegrityError."""
        rec = self._anchor_rec()
        c = self._conn()
        c.execute("BEGIN")
        try:
            rows = c.execute("SELECT seq, kind, op_id, body, prev, hash FROM oplog_chain ORDER BY seq").fetchall()
            prev, expect, last_by_op = GENESIS, 1, {}
            for r in rows:
                if r["seq"] != expect:
                    raise OpLogIntegrityError(f"{self.path}: the operation chain has a gap or is reordered at record {expect} (found {r['seq']})")
                if r["prev"] != prev or _rec_hash(r["prev"], r["seq"], r["kind"], r["op_id"], r["body"]) != r["hash"]:
                    raise OpLogIntegrityError(f"{self.path}: the operation chain's hash chain is broken at record {r['seq']}")
                prev, expect = r["hash"], expect + 1
                if r["kind"] not in ("case", "case_release", "stale"):
                    last_by_op[r["op_id"]] = json.loads(r["body"])
            self._check_anchor(c, len(rows), rec)
            if not rows and c.execute("SELECT COUNT(*) FROM ops").fetchone()[0]:
                raise OpLogIntegrityError(f"{self.path}: operation rows exist but the chain is empty: not a chained ledger (cannot adopt)")
            seen_ops = set()
            for row in c.execute("SELECT * FROM ops").fetchall():
                seen_ops.add(row["op_id"])
                if last_by_op.get(row["op_id"]) != self._body_of(row):
                    raise OpLogIntegrityError(f"{self.path}: operation {row['op_id']} does not match the chain (edited or replaced)")
            missing = set(last_by_op) - seen_ops
            if missing:
                raise OpLogIntegrityError(f"{self.path}: operation(s) {sorted(missing)[:3]} are in the chain but not in the ledger (deleted)")
        finally:
            c.execute("ROLLBACK")
        with self._tlock:
            self._seen = (len(rows), prev) if rows else None
        return {"ok": True, "records": len(rows), "anchored": (rec or {}).get("count", 0), "anchor_key": "configured" if (self._anchor and self._anchor.key) else "none configured"}

    def _check_op(self, c, op: str) -> None:
        """Incremental check inside the caller's write transaction (verify-before-claim)."""
        rec = self._anchor_rec()
        with self._tlock:
            seen = self._seen
        if seen is not None:
            row = c.execute("SELECT hash FROM oplog_chain WHERE seq=?", (seen[0],)).fetchone()
            if row is None or row["hash"] != seen[1]:
                raise OpLogIntegrityError(f"{self.path}: a verified operation record was removed or changed (ledger truncated or rewritten)")
        start = seen[0] if seen else 0
        prev = seen[1] if seen else GENESIS
        new = c.execute("SELECT seq, kind, op_id, body, prev, hash FROM oplog_chain WHERE seq>? ORDER BY seq", (start,)).fetchall()
        expect = start + 1
        for r in new:
            if r["seq"] != expect or r["prev"] != prev or _rec_hash(r["prev"], r["seq"], r["kind"], r["op_id"], r["body"]) != r["hash"]:
                raise OpLogIntegrityError(f"{self.path}: the operation chain's hash chain is broken at record {r['seq']}")
            prev, expect = r["hash"], expect + 1
        self._check_anchor(c, expect - 1, rec)
        row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
        last = c.execute("SELECT body FROM oplog_chain WHERE op_id=? AND kind NOT IN ('case','case_release','stale') ORDER BY seq DESC LIMIT 1", (op,)).fetchone()
        if row is None and last is not None:
            raise OpLogIntegrityError(f"{self.path}: operation {op} is in the chain but not in the ledger (deleted)")
        if row is not None and (last is None or json.loads(last["body"]) != self._body_of(row)):
            raise OpLogIntegrityError(f"{self.path}: operation {op} does not match the chain (edited or replaced)")
        with self._tlock:
            if new and (self._seen is None or self._seen[0] < expect - 1):
                self._seen = (expect - 1, prev)

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

    def stale_results(self, op: str) -> list:
        return [json.loads(r["body"]) for r in self._conn().execute("SELECT body FROM oplog_chain WHERE op_id=? AND kind='stale' ORDER BY seq", (op,))]

    # ---- cases: one mission per (case key, attempt), across processes -------------------------------------------------------
    def open_case(self, case_key: str, attempt: int) -> "tuple[bool, str | None]":
        """(True, None) for the caller that must create the mission; (False, mission_id) for every later submit of the same case/attempt
        (mission_id is None while the creator has not finished: the caller must not create a second one)."""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO cases (case_key, attempt, mission_id, created) VALUES (?,?,NULL,?)", (case_key, int(attempt), self.clock()))
            if cur.rowcount == 1:
                self._append(c, "case", f"{case_key}#{int(attempt)}", {"mission_id": None})
                return True, None
            row = c.execute("SELECT mission_id FROM cases WHERE case_key=? AND attempt=?", (case_key, int(attempt))).fetchone()
            return False, row["mission_id"]

    def bind_case(self, case_key: str, attempt: int, mission_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE cases SET mission_id=? WHERE case_key=? AND attempt=? AND mission_id IS NULL", (mission_id, case_key, int(attempt)))
            self._append(c, "case", f"{case_key}#{int(attempt)}", {"mission_id": mission_id})

    def release_case(self, case_key: str, attempt: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM cases WHERE case_key=? AND attempt=? AND mission_id IS NULL", (case_key, int(attempt)))
            self._append(c, "case_release", f"{case_key}#{int(attempt)}", {})

    # ---- transitions ------------------------------------------------------------------------------------------------
    def plan_op(self, op: str, mission_id: str, step: str, app: str, capability: str, target_digest: str, attempt: int = 1) -> dict:
        with self._tx() as c:
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is not None:
                self._check_op(c, op)
                if (row["mission_id"], row["step"], row["target_digest"], row["capability"]) != (mission_id, step, target_digest, capability):
                    raise OpConflict(f"{op} already exists for a different mission/step/target")
                return self._d(row)
            now = self.clock()
            c.execute("INSERT INTO ops (op_id, mission_id, step, app, capability, target_digest, attempt, state, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (op, mission_id, step, app, capability, target_digest, int(attempt), PLANNED, now, now))
            self._hist(c, op, PLANNED)
            self._log_op(c, "plan", op)
            return self._d(c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone())

    def claim(self, op: str, owner: str) -> Claim:
        """Take the right to send the request for ``op`` (persisted before any request, verified before it is granted), or learn why not.
        Every ``acquired`` claim has a NEW generation (``fence``); whoever holds an older one is stale."""
        with self._tx() as c:
            self._check_op(c, op)
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
            if st == INFLIGHT and row["owner"] != owner:
                return Claim("held", op, row["fence"], row["owner"] or "", self._d(row))
            fence = row["fence"] + 1                          # a re-entry by the same executor is a new generation too
            c.execute("UPDATE ops SET state=?, owner=?, fence=?, lease_until=?, updated=? WHERE op_id=?", (INFLIGHT, owner, fence, now + self.lease_ttl_s, now, op))
            self._hist(c, op, INFLIGHT, f"owner={owner} fence={fence}")
            self._log_op(c, "claim", op)
            return Claim("acquired", op, fence, owner, self._d(c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()))

    def _fenced(self, c, op: str, fence: int, states=(INFLIGHT,)) -> sqlite3.Row:
        row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
        if row is None or row["fence"] != fence or row["state"] not in states:
            raise FencingError(f"{op}: generation {fence} is no longer the current claim")
        return row

    def check_fence(self, op: str, fence: int) -> None:
        """Called immediately before a request: raises FencingError if the caller's generation was superseded or its lease ran out."""
        with self._tx() as c:
            self._check_op(c, op)
            row = self._fenced(c, op, fence)
            if (row["lease_until"] or 0) <= self.clock():
                raise FencingError(f"{op}: generation {fence} lease expired before the request was sent")

    def record_applied(self, op: str, fence: int, native_id: str, result: dict, note: str = "") -> None:
        with self._tx() as c:
            self._fenced(c, op, fence)
            c.execute("UPDATE ops SET state=?, native_id=?, result=?, error=NULL, lease_until=NULL, updated=? WHERE op_id=?",
                      (APPLIED, str(native_id), json.dumps(result, default=str), self.clock(), op))
            self._hist(c, op, APPLIED, note or str(native_id))
            self._log_op(c, "applied", op)

    def record_unknown(self, op: str, fence: int, error: str) -> None:
        with self._tx() as c:
            self._fenced(c, op, fence)
            c.execute("UPDATE ops SET state=?, error=?, updated=? WHERE op_id=?", (UNKNOWN, error[:500], self.clock(), op))
            self._hist(c, op, UNKNOWN, error)
            self._log_op(c, "unknown", op)

    def record_refused(self, op: str, fence: int, error: str) -> None:
        """A definite refusal by the application (the request did not take effect): retryable under the same op id."""
        with self._tx() as c:
            self._fenced(c, op, fence)
            c.execute("UPDATE ops SET state=?, error=?, lease_until=NULL, updated=? WHERE op_id=?", (PLANNED, error[:500], self.clock(), op))
            self._hist(c, op, PLANNED, "refused: " + error)
            self._log_op(c, "refused", op)

    def record_stale(self, op: str, fence: int, native_id: str, result: dict, note: str = "") -> None:
        """A holder that no longer owns the claim reports a result (its request reached the application after it lost the lease). The
        operation's state is NOT changed: the result is kept as ``stale-generation`` evidence so a reconciliation can see it."""
        with self._tx() as c:
            self._check_op(c, op)
            cur = c.execute("SELECT fence, state FROM ops WHERE op_id=?", (op,)).fetchone()
            self._hist(c, op, STALE, f"generation {fence} (current {cur['fence'] if cur else '?'}, {cur['state'] if cur else '?'}): {native_id} {note}")
            self._append(c, "stale", op, {"stale_fence": fence, "current_fence": cur["fence"] if cur else None, "native_id": str(native_id), "result_sha256": _sha(result), "note": note[:200]})

    def resolve_applied(self, op: str, native_id: str, result: dict, note: str) -> None:
        """Reconciliation FOUND the record in the application (by marker): adopt it. No second write."""
        with self._tx() as c:
            self._check_op(c, op)
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is None or row["state"] not in (UNKNOWN, INFLIGHT, PLANNED):
                raise OpError(f"{op}: nothing to resolve")
            c.execute("UPDATE ops SET state=?, native_id=?, result=?, error=NULL, lease_until=NULL, fence=fence+1, updated=? WHERE op_id=?",
                      (APPLIED, str(native_id), json.dumps(result, default=str), self.clock(), op))
            self._hist(c, op, APPLIED, "adopted: " + note)
            self._log_op(c, "resolve_applied", op)

    def resolve_not_applied(self, op: str, note: str) -> None:
        """Reconciliation found NOTHING in the application: the op may be claimed again (the generation advances, stale executors are fenced off)."""
        with self._tx() as c:
            self._check_op(c, op)
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            if row is None or row["state"] not in (UNKNOWN, INFLIGHT):
                raise OpError(f"{op}: nothing to resolve")
            c.execute("UPDATE ops SET state=?, owner=NULL, lease_until=NULL, fence=fence+1, updated=? WHERE op_id=?", (PLANNED, self.clock(), op))
            self._hist(c, op, PLANNED, "not applied: " + note)
            self._log_op(c, "resolve_not_applied", op)

    def begin_compensation(self, op: str) -> "int | None":
        """Start an undo: a NEW generation, returned to the caller. None if there is nothing to undo, or another compensation holds an
        unexpired lease (a compensator that died leaves the lease to run out; undoing is idempotent for all four applications, so then a
        new generation may take over)."""
        with self._tx() as c:
            self._check_op(c, op)
            row = c.execute("SELECT * FROM ops WHERE op_id=?", (op,)).fetchone()
            now = self.clock()
            ok = row is not None and (row["state"] in (APPLIED, COMP_FAILED) or (row["state"] == COMPENSATING and (row["lease_until"] or 0) <= now))
            if not ok:
                return None
            fence = row["fence"] + 1
            c.execute("UPDATE ops SET state=?, fence=?, lease_until=?, updated=? WHERE op_id=?", (COMPENSATING, fence, now + self.lease_ttl_s, now, op))
            self._hist(c, op, COMPENSATING, f"fence={fence}")
            self._log_op(c, "compensating", op)
            return fence

    def end_compensation(self, op: str, fence: int, ok: bool, note: str = "") -> None:
        with self._tx() as c:
            self._fenced(c, op, fence, states=(COMPENSATING,))
            c.execute("UPDATE ops SET state=?, error=?, lease_until=NULL, updated=? WHERE op_id=?", (COMPENSATED if ok else COMP_FAILED, None if ok else note[:500], self.clock(), op))
            self._hist(c, op, COMPENSATED if ok else COMP_FAILED, note)
            self._log_op(c, "compensated" if ok else "comp_failed", op)
