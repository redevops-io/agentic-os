"""The durable record behind the browser UI and the `/api/v1` API: cases, evidence, actions, decisions, receipts with their
persisted read-back, an append-only history that doubles as the change feed, and the local users and sessions.

One SQLite file (`SENTINEL_STATE_DIR/sentinel.db`, mode 0600, WAL, synchronous FULL; standard library only). Without a state
directory the database is in memory and the process says so in `/api/v1/info` (`persistent: false`).

What it is and is not:
  * It is the READ MODEL and the AUDIT TRAIL. The case spine itself (evidence -> observation -> finding -> action) is still derived
    from CrowdSec alerts by `incident.py` and held by `case_store.CaseStore`; `sync_store` snapshots it here so a case stays visible
    after CrowdSec expires the alert. `cases-state.json` (statefile.py) keeps its role as the overlay that restores action states on
    restart; both are written under the same lock and a failure of either refuses the decision (memory never runs ahead of disk).
  * Evidence is content addressed: a row is never replaced with different bytes (IntegrityError), exactly like `CaseStore.put_evidence`.
  * Nothing here authorises anything. Roles and sessions live in `identity.py`, which uses the same database.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from .evidence import canonical_json, sha256_hex

SCHEMA_VERSION = 1
_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases(
  case_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
  case_type TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL, bundle_json TEXT NOT NULL, bundle_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(
  artifact_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, acquired_at TEXT NOT NULL, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS case_evidence(case_id TEXT NOT NULL, artifact_id TEXT NOT NULL, PRIMARY KEY(case_id, artifact_id));
CREATE TABLE IF NOT EXISTS actions(
  request_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, capability TEXT NOT NULL, params_json TEXT NOT NULL, digest TEXT NOT NULL,
  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS actions_state ON actions(state);
CREATE TABLE IF NOT EXISTS decisions(
  decision_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, actor_user TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT '', via TEXT NOT NULL DEFAULT '',
  external_actor TEXT NOT NULL DEFAULT '', approved INTEGER NOT NULL, rationale TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(
  receipt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, decision_id TEXT NOT NULL, status TEXT NOT NULL, external_ref TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0,
  verification_state TEXT NOT NULL DEFAULT '', verification_detail TEXT NOT NULL DEFAULT '', verified_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS history(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, type TEXT NOT NULL, case_id TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
  actor_user TEXT NOT NULL DEFAULT '', via TEXT NOT NULL DEFAULT '', detail_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS users(
  user_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', disabled INTEGER NOT NULL DEFAULT 0, last_login_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL, created REAL NOT NULL, last_seen REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS actor_links(
  external_actor TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
"""


class StoreUnavailable(RuntimeError):
    """The durable store could not be read or written; callers refuse the operation instead of continuing in memory."""


class IntegrityError(RuntimeError):
    """An evidence row would be replaced with different bytes."""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def action_digest(capability: str, parameters: dict, finding_refs=(), evidence_refs=()) -> str:
    """The approval binds THIS content: the same basis as `ActionRequest.request_id`, at full length."""
    return sha256_hex(canonical_json({"cap": capability, "params": parameters, "fnd": sorted(finding_refs), "ev": sorted(evidence_refs)}))


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(int(offset)).encode()).decode().rstrip("=")


def decode_cursor(cursor: "str | None") -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()))
    except Exception:  # noqa: BLE001
        raise ValueError("invalid cursor") from None


class HistoryStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.persistent = bool(path)
        self.lock = threading.RLock()
        try:
            if path:
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                fresh = not os.path.exists(path)
                self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
                if fresh:
                    os.chmod(path, 0o600)
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
            else:
                self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.executescript(_SCHEMA)
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        except (sqlite3.Error, OSError) as e:
            raise StoreUnavailable(f"history store unavailable ({type(e).__name__}: {e})") from None

    # ── plumbing ────────────────────────────────────────────────────────────────────────────────────────────
    def run(self, sql: str, args: tuple = ()) -> "_Result":
        """Execute and return what callers may need (rowcount, lastrowid, fetched rows), all read INSIDE the lock: the connection is shared
        by the request threads, and a cursor read after the lock is released can see another thread's statement."""
        with self.lock:
            try:
                cur = self.db.execute(sql, args)
                return _Result(cur.rowcount, cur.lastrowid, cur.fetchall() if cur.description else [])
            except sqlite3.IntegrityError:
                raise
            except sqlite3.Error as e:
                raise StoreUnavailable(f"history store error ({type(e).__name__})") from None

    def rows(self, sql: str, args: tuple = ()) -> list:
        return [dict(r) for r in self.run(sql, args).fetched]

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        got = self.run(sql, args).fetched
        return dict(got[0]) if got else None

    def transaction(self):
        return _Tx(self)

    def health(self) -> dict:
        try:
            self.run("SELECT 1")
            return {"ok": True, "persistent": self.persistent}
        except StoreUnavailable as e:
            return {"ok": False, "persistent": self.persistent, "error": str(e)}

    # ── history / change feed ───────────────────────────────────────────────────────────────────────────────
    def append(self, type_: str, *, case_id: str = "", request_id: str = "", actor_user: str = "", via: str = "", detail: Optional[dict] = None) -> int:
        cur = self.run("INSERT INTO history(at, type, case_id, request_id, actor_user, via, detail_json) VALUES(?,?,?,?,?,?,?)",
                       (now_iso(), type_, case_id, request_id, actor_user, via, canonical_json(detail or {})))
        return int(cur.lastrowid)

    def changes(self, after: int = 0, limit: int = 100) -> dict:
        limit = max(1, min(int(limit), 200))
        items = self.rows("SELECT seq, at, type, case_id, request_id, actor_user AS actor FROM history WHERE seq > ? ORDER BY seq LIMIT ?", (int(after), limit))
        head = self.one("SELECT COALESCE(MAX(seq), 0) AS head FROM history")["head"]
        return {"items": items, "head": head}

    def history(self, *, type_: str = "", actor: str = "", limit: int = 50, cursor: "str | None" = None) -> dict:
        limit = max(1, min(int(limit), 200))
        off = decode_cursor(cursor)
        where, args = [], []
        if type_:
            where.append("type = ?"); args.append(type_)
        if actor:
            where.append("actor_user = ?"); args.append(actor)
        sql = "SELECT seq, at, type, case_id, request_id, actor_user AS actor, via, detail_json FROM history"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self.rows(sql + " ORDER BY seq DESC LIMIT ? OFFSET ?", (*args, limit + 1, off))
        more = len(rows) > limit
        items = [{**{k: v for k, v in r.items() if k != "detail_json"}, "detail": json.loads(r["detail_json"] or "{}")} for r in rows[:limit]]
        return {"items": items, "next_cursor": encode_cursor(off + limit) if more else None}

    # ── snapshot of the live case store ─────────────────────────────────────────────────────────────────────
    def sync_store(self, st, *, attempts: "dict | None" = None) -> list:
        """Persist what the case store holds now: new cases and evidence, changed bundles, action states. Returns the new case ids."""
        attempts = attempts or {}
        new_cases: list = []
        with self.lock, self.transaction():
            for case_id, case in st.cases.items():
                bundle = st.case_bundle(case_id)
                text = canonical_json(bundle)
                h = sha256_hex(text)
                d = case.to_dict()
                row = self.one("SELECT bundle_hash, status, severity FROM cases WHERE case_id=?", (case_id,))
                ts = now_iso()
                if row is None:
                    self.run("INSERT INTO cases(case_id, first_seen, last_seen, severity, status, case_type, title, created_at, bundle_json, bundle_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (case_id, ts, ts, d["severity"], d["status"], d["case_type"], d["title"], d.get("created_at", ""), text, h))
                    self.append("case.opened", case_id=case_id, actor_user="system", via="crowdsec", detail={"severity": d["severity"], "title": d["title"]})
                    new_cases.append(case_id)
                else:
                    self.run("UPDATE cases SET last_seen=?, severity=?, status=?, bundle_json=?, bundle_hash=? WHERE case_id=?", (ts, d["severity"], d["status"], text, h, case_id))
                    if row["status"] != d["status"]:
                        self.append("case.status", case_id=case_id, actor_user="system", via="sentinel", detail={"from": row["status"], "to": d["status"]})
                for eid in case.evidence_refs:
                    ev = st.evidence.get(eid)
                    if ev is None:
                        continue
                    raw = st.get_raw(ev)
                    if raw is None:
                        continue
                    old = self.one("SELECT content FROM evidence WHERE artifact_id=?", (eid,))
                    if old is not None and old["content"] != raw:
                        raise IntegrityError(f"{eid}: append would overwrite different evidence bytes")
                    if old is None:
                        self.run("INSERT INTO evidence(artifact_id, sha256, kind, source, acquired_at, content) VALUES(?,?,?,?,?,?)", (eid, ev.sha256, ev.kind, ev.source, ev.acquired_at, raw))
                    self.run("INSERT OR IGNORE INTO case_evidence(case_id, artifact_id) VALUES(?,?)", (case_id, eid))
                for rid in case.action_refs:
                    a = st.actions.get(rid)
                    if a is None:
                        continue
                    att = attempts.get(rid, {"n": 0, "last": 0})
                    cur = self.one("SELECT state FROM actions WHERE request_id=?", (rid,))
                    digest = action_digest(a.capability, a.parameters, a.finding_refs, a.evidence_refs)
                    if cur is None:
                        self.run("INSERT INTO actions(request_id, case_id, capability, params_json, digest, state, attempts, last_attempt_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (rid, case_id, a.capability, canonical_json(a.parameters), digest, a.state.value, int(att.get("n", 0)), float(att.get("last", 0)), a.created_at, ts))
                        self.append("action.proposed", case_id=case_id, request_id=rid, actor_user="system", via="sentinel", detail={"capability": a.capability, "parameters": a.parameters})
                    elif cur["state"] != a.state.value:
                        self.run("UPDATE actions SET state=?, attempts=?, last_attempt_at=?, updated_at=? WHERE request_id=?", (a.state.value, int(att.get("n", 0)), float(att.get("last", 0)), ts, rid))
                    else:
                        self.run("UPDATE actions SET attempts=?, last_attempt_at=? WHERE request_id=?", (int(att.get("n", 0)), float(att.get("last", 0)), rid))
        return new_cases

    def record_decision(self, d: dict, *, case_id: str, actor_role: str = "", via: str = "", external_actor: str = "") -> None:
        self.run("INSERT OR REPLACE INTO decisions(decision_id, request_id, actor_user, actor_role, via, external_actor, approved, rationale, at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (d["decision_id"], d["request_id"], d["actor"], actor_role, via, external_actor, 1 if d["approved"] else 0, d.get("rationale", ""), d["at"]))
        self.append("action.decided", case_id=case_id, request_id=d["request_id"], actor_user=d["actor"], via=via,
                    detail={"approved": bool(d["approved"]), "role": actor_role, "external_actor": external_actor})

    def record_receipt(self, r: dict, *, case_id: str, actor_user: str, via: str, dry_run: bool, verification: Optional[dict]) -> None:
        v = verification or {}
        self.run("INSERT OR REPLACE INTO receipts(receipt_id, request_id, decision_id, status, external_ref, error, at, dry_run, verification_state, verification_detail, verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (r["receipt_id"], r["request_id"], r["decision_id"], r["status"], r.get("external_ref", ""), r.get("error", ""), r["at"], 1 if dry_run else 0,
                  v.get("state", ""), v.get("detail", ""), v.get("at", "")))
        etype = "action.executed" if r["status"] in ("SUCCEEDED", "DRY_RUN") else "action.failed"
        self.append(etype, case_id=case_id, request_id=r["request_id"], actor_user=actor_user, via=via, detail={"status": r["status"], "dry_run": dry_run})
        if v.get("state"):
            self.append("action.verified", case_id=case_id, request_id=r["request_id"], actor_user=actor_user, via=via, detail={"state": v["state"]})

    # ── reads ───────────────────────────────────────────────────────────────────────────────────────────────
    def _case_row(self, r: dict) -> dict:
        bundle = json.loads(r["bundle_json"])
        c = bundle["case"]
        acts = self.rows("SELECT request_id, capability, params_json, state FROM actions WHERE case_id=?", (r["case_id"],))
        open_a = next(({"request_id": a["request_id"], "capability": a["capability"], "parameters": json.loads(a["params_json"]), "state": a["state"]}
                       for a in acts if a["state"] in ("AWAITING_APPROVAL", "APPROVED", "FAILED")), None)
        return {"case_id": r["case_id"], "case_type": r["case_type"], "severity": r["severity"], "status": r["status"], "title": r["title"],
                "created_at": r["created_at"], "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                "counts": {"evidence": len(c.get("evidence_refs", [])), "findings": len(c.get("finding_refs", [])),
                           "actions": len(c.get("action_refs", [])), "decisions": len(c.get("decision_refs", []))},
                "open_action": open_a}

    def list_cases(self, *, status: str = "", severity: str = "", q: str = "", limit: int = 50, cursor: "str | None" = None) -> dict:
        limit = max(1, min(int(limit), 200))
        off = decode_cursor(cursor)
        where, args = [], []
        if status:
            where.append("status = ?"); args.append(status)
        if severity:
            where.append("severity = ?"); args.append(severity)
        if q:
            where.append("(title LIKE ? ESCAPE '\\' OR case_id LIKE ? ESCAPE '\\')")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like, like]
        sql = "SELECT * FROM cases" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.rows(sql, tuple(args))
        rows.sort(key=lambda r: (_SEV_RANK.get(r["severity"], 9), r["created_at"]), reverse=False)
        page = rows[off: off + limit]
        return {"items": [self._case_row(r) for r in page], "next_cursor": encode_cursor(off + limit) if len(rows) > off + limit else None}

    def get_case(self, case_id: str) -> Optional[dict]:
        r = self.one("SELECT * FROM cases WHERE case_id=?", (case_id,))
        if r is None:
            return None
        bundle = json.loads(r["bundle_json"])
        summary = self._case_row(r)
        actions = [self.get_action(a["request_id"]) for a in self.rows("SELECT request_id FROM actions WHERE case_id=? ORDER BY created_at", (case_id,))]
        return {**bundle, "summary": summary, "actions_detail": actions,
                "history": self.rows("SELECT seq, at, type, request_id, actor_user AS actor, via FROM history WHERE case_id=? ORDER BY seq", (case_id,))}

    def case_has_evidence(self, case_id: str, artifact_id: str) -> bool:
        return self.one("SELECT 1 AS x FROM case_evidence WHERE case_id=? AND artifact_id=?", (case_id, artifact_id)) is not None

    def get_evidence(self, artifact_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM evidence WHERE artifact_id=?", (artifact_id,))

    def get_action(self, request_id: str) -> Optional[dict]:
        a = self.one("SELECT * FROM actions WHERE request_id=?", (request_id,))
        if a is None:
            return None
        dec = self.one("SELECT * FROM decisions WHERE request_id=? ORDER BY at DESC, rowid DESC LIMIT 1", (request_id,))
        rec = self.one("SELECT * FROM receipts WHERE request_id=? ORDER BY at DESC, rowid DESC LIMIT 1", (request_id,))
        decision = None if dec is None else {"decision_id": dec["decision_id"], "actor": dec["actor_user"], "role": dec["actor_role"], "via": dec["via"],
                                            "external_actor": dec["external_actor"], "approved": bool(dec["approved"]), "rationale": dec["rationale"], "at": dec["at"]}
        receipt = verification = None
        if rec is not None:
            receipt = {"receipt_id": rec["receipt_id"], "status": rec["status"], "external_ref": rec["external_ref"], "error": rec["error"], "at": rec["at"], "dry_run": bool(rec["dry_run"])}
            if rec["verification_state"]:
                verification = {"state": rec["verification_state"], "detail": rec["verification_detail"], "at": rec["verified_at"]}
        return {"request_id": a["request_id"], "case_id": a["case_id"], "capability": a["capability"], "params": json.loads(a["params_json"]), "digest": a["digest"],
                "state": a["state"], "attempts": a["attempts"], "created_at": a["created_at"], "updated_at": a["updated_at"],
                "decision": decision, "receipt": receipt, "verification": verification}

    def list_actions(self, *, state: str = "", limit: int = 50, cursor: "str | None" = None) -> dict:
        limit = max(1, min(int(limit), 200))
        off = decode_cursor(cursor)
        rows = self.rows("SELECT request_id FROM actions" + (" WHERE state = ?" if state else "") + " ORDER BY updated_at DESC, request_id LIMIT ? OFFSET ?",
                         ((state,) if state else ()) + (limit + 1, off))
        more = len(rows) > limit
        return {"items": [self.get_action(r["request_id"]) for r in rows[:limit]], "next_cursor": encode_cursor(off + limit) if more else None}


class _Result:
    __slots__ = ("rowcount", "lastrowid", "fetched")

    def __init__(self, rowcount, lastrowid, fetched):
        self.rowcount, self.lastrowid, self.fetched = rowcount, lastrowid, fetched


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, ROLLBACK on error. Re-entrant on one thread (the RLock) by nesting as a no-op."""
    depth = threading.local()

    def __init__(self, store: HistoryStore):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        d = getattr(self.depth, "n", 0)
        self.depth.n = d + 1
        self.outer = d == 0
        if self.outer:
            try:
                self.store.db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as e:
                self.depth.n = d
                self.store.lock.release()
                raise StoreUnavailable(f"history store error ({type(e).__name__})") from None
        return self

    def __exit__(self, et, ev, tb):
        try:
            if self.outer:
                self.store.db.execute("COMMIT" if et is None else "ROLLBACK")
        except sqlite3.Error as e:
            if et is None:
                raise StoreUnavailable(f"history store error ({type(e).__name__})") from None
        finally:
            self.depth.n -= 1
            self.store.lock.release()
        return False


# ── process-wide handle ─────────────────────────────────────────────────────────────────────────────────────
_STORE: Optional[HistoryStore] = None
_STORE_LOCK = threading.Lock()


def get() -> HistoryStore:
    """The process store for the configured SENTINEL_STATE_DIR (reopened if the setting changed, which only tests do)."""
    global _STORE
    with _STORE_LOCK:
        d = os.environ.get("SENTINEL_STATE_DIR") or None
        path = os.path.join(d, "sentinel.db") if d else None
        if _STORE is None or _STORE.path != path:
            if _STORE is not None:
                try:
                    _STORE.db.close()
                except sqlite3.Error:
                    pass
            _STORE = HistoryStore(path)
        return _STORE


def reset() -> None:
    """Test hook: close and forget the process store (the next `get()` opens the configured one)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            try:
                _STORE.db.close()
            except sqlite3.Error:
                pass
        _STORE = None
