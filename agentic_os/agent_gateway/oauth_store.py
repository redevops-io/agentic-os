"""Where the authorization server keeps what it has issued: in memory, or durably in SQLite.

Nothing stored here can be presented as a credential: codes and tokens are kept only as SHA-256 hashes. A
**connection** is what a person approved (this client, for this tenant, with these grants, until this time); every code
and token belongs to one, and revoking it ends them all.

The two operations that must happen once are atomic across processes in the SQLite store: taking an authorization code,
and spending a refresh token. A refresh token presented a second time is reported as ``reused``, which the server treats
as theft of the connection.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Tuple

Record = Dict[str, Any]


class OAuthStore(Protocol):
    def put_client(self, client_id: str, doc: Record) -> None: ...
    def get_client(self, client_id: str) -> Optional[Record]: ...
    def count_clients(self) -> int: ...
    def put_connection(self, doc: Record) -> None: ...
    def get_connection(self, connection_id: str) -> Optional[Record]: ...
    def connections(self) -> List[Record]: ...
    def revoke_connection(self, connection_id: str) -> bool: ...
    def put_code(self, digest: str, doc: Record) -> None: ...
    def take_code(self, digest: str) -> Optional[Record]: ...
    def put_tokens(self, access_digest: str, access: Record, refresh_digest: str, refresh: Record) -> None: ...
    def get_access(self, digest: str) -> Optional[Record]: ...
    def drop_access(self, digest: str) -> None: ...
    def get_refresh(self, digest: str) -> Optional[Record]: ...
    def spend_refresh(self, digest: str) -> Tuple[Optional[Record], str]: ...
    def purge(self, now: float) -> None: ...


class MemoryOAuthStore:
    """Per process; a restart forgets every connection. For tests and local development."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: Dict[str, Record] = {}
        self._connections: Dict[str, Record] = {}
        self._codes: Dict[str, Record] = {}
        self._access: Dict[str, Record] = {}
        self._refresh: Dict[str, Record] = {}
        self._spent: Dict[str, Record] = {}

    def put_client(self, client_id, doc):
        self._clients[client_id] = dict(doc)

    def get_client(self, client_id):
        return self._clients.get(client_id)

    def count_clients(self):
        return len(self._clients)

    def put_connection(self, doc):
        self._connections[doc["connection_id"]] = dict(doc)

    def get_connection(self, connection_id):
        got = self._connections.get(connection_id)
        return dict(got) if got else None

    def connections(self):
        return [dict(c) for c in self._connections.values()]

    def revoke_connection(self, connection_id):
        with self._lock:
            c = self._connections.get(connection_id)
            if c is None:
                return False
            c["revoked"] = True
            for table in (self._access, self._refresh, self._codes):
                for k in [k for k, v in table.items() if v.get("connection_id") == connection_id]:
                    del table[k]
            return True

    def put_code(self, digest, doc):
        self._codes[digest] = dict(doc)

    def take_code(self, digest):
        with self._lock:
            return self._codes.pop(digest, None)

    def put_tokens(self, access_digest, access, refresh_digest, refresh):
        with self._lock:
            for k in [k for k, v in self._access.items() if v.get("connection_id") == access.get("connection_id")]:
                del self._access[k]                       # one live access token per connection
            self._access[access_digest] = dict(access)
            self._refresh[refresh_digest] = dict(refresh)

    def get_access(self, digest):
        return self._access.get(digest)

    def drop_access(self, digest):
        self._access.pop(digest, None)

    def get_refresh(self, digest):
        return self._refresh.get(digest)

    def spend_refresh(self, digest):
        with self._lock:
            rec = self._refresh.pop(digest, None)
            if rec is not None:
                self._spent[digest] = rec
                return rec, "ok"
            spent = self._spent.get(digest)
            return (spent, "reused") if spent is not None else (None, "unknown")

    def purge(self, now):
        with self._lock:
            for table in (self._codes, self._access, self._refresh, self._spent):
                for k in [k for k, v in table.items() if v["exp"] <= now]:
                    del table[k]


class SqliteOAuthStore:
    """Connections survive restarts and are shared by workers on one host. A storage error raises, and the server
    answers it by refusing: nothing is ever admitted because the store could not be read."""

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, doc TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, doc TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS codes (digest TEXT PRIMARY KEY, connection_id TEXT NOT NULL, exp REAL NOT NULL, doc TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS access (digest TEXT PRIMARY KEY, connection_id TEXT NOT NULL, exp REAL NOT NULL, doc TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS refresh (digest TEXT PRIMARY KEY, connection_id TEXT NOT NULL, exp REAL NOT NULL, doc TEXT NOT NULL, spent INTEGER NOT NULL DEFAULT 0)",
    )

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        if not os.path.exists(path):
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self._retry(lambda: self._db.execute("PRAGMA journal_mode=WAL"))
        for statement in self._SCHEMA:
            self._retry(lambda s=statement: self._db.execute(s))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _retry(fn, *, attempts: int = 60, delay: float = 0.05):
        for i in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if ("locked" not in msg and "busy" not in msg) or i == attempts - 1:
                    raise
                time.sleep(min(delay * (1 + i // 5), 0.5))

    def _run(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return self._retry(lambda: self._db.execute(sql, args).fetchall())

    def _atomically(self, fn):
        """``fn`` inside one write transaction: no other process can act between what it reads and what it writes."""
        with self._lock:
            self._retry(lambda: self._db.execute("BEGIN IMMEDIATE"))
            try:
                out = fn(self._db)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._retry(lambda: self._db.execute("COMMIT"))
            return out

    def put_client(self, client_id, doc):
        self._run("INSERT OR REPLACE INTO clients (id, doc) VALUES (?, ?)", (client_id, json.dumps(doc)))

    def get_client(self, client_id):
        rows = self._run("SELECT doc FROM clients WHERE id = ?", (client_id,))
        return json.loads(rows[0][0]) if rows else None

    def count_clients(self):
        return int(self._run("SELECT COUNT(*) FROM clients")[0][0])

    def put_connection(self, doc):
        self._run("INSERT OR REPLACE INTO connections (id, doc) VALUES (?, ?)", (doc["connection_id"], json.dumps(doc)))

    def get_connection(self, connection_id):
        rows = self._run("SELECT doc FROM connections WHERE id = ?", (connection_id,))
        return json.loads(rows[0][0]) if rows else None

    def connections(self):
        return [json.loads(r[0]) for r in self._run("SELECT doc FROM connections")]

    def revoke_connection(self, connection_id):
        def revoke(db):
            rows = db.execute("SELECT doc FROM connections WHERE id = ?", (connection_id,)).fetchall()
            if not rows:
                return False
            doc = {**json.loads(rows[0][0]), "revoked": True}
            db.execute("UPDATE connections SET doc = ? WHERE id = ?", (json.dumps(doc), connection_id))
            for table in ("codes", "access"):
                db.execute(f"DELETE FROM {table} WHERE connection_id = ?", (connection_id,))
            db.execute("UPDATE refresh SET spent = 1 WHERE connection_id = ?", (connection_id,))
            return True
        return self._atomically(revoke)

    def put_code(self, digest, doc):
        self._run("INSERT INTO codes (digest, connection_id, exp, doc) VALUES (?, ?, ?, ?)",
                  (digest, doc["connection_id"], doc["exp"], json.dumps(doc)))

    def take_code(self, digest):
        def take(db):
            rows = db.execute("SELECT doc FROM codes WHERE digest = ?", (digest,)).fetchall()
            db.execute("DELETE FROM codes WHERE digest = ?", (digest,))
            return json.loads(rows[0][0]) if rows else None
        return self._atomically(take)

    def put_tokens(self, access_digest, access, refresh_digest, refresh):
        def put(db):
            db.execute("DELETE FROM access WHERE connection_id = ?", (access["connection_id"],))
            db.execute("INSERT INTO access (digest, connection_id, exp, doc) VALUES (?, ?, ?, ?)",
                       (access_digest, access["connection_id"], access["exp"], json.dumps(access)))
            db.execute("INSERT INTO refresh (digest, connection_id, exp, doc) VALUES (?, ?, ?, ?)",
                       (refresh_digest, refresh["connection_id"], refresh["exp"], json.dumps(refresh)))
        self._atomically(put)

    def get_access(self, digest):
        rows = self._run("SELECT doc FROM access WHERE digest = ?", (digest,))
        return json.loads(rows[0][0]) if rows else None

    def drop_access(self, digest):
        self._run("DELETE FROM access WHERE digest = ?", (digest,))

    def get_refresh(self, digest):
        rows = self._run("SELECT doc FROM refresh WHERE digest = ? AND spent = 0", (digest,))
        return json.loads(rows[0][0]) if rows else None

    def spend_refresh(self, digest):
        def spend(db):
            rows = db.execute("SELECT doc, spent FROM refresh WHERE digest = ?", (digest,)).fetchall()
            if not rows:
                return None, "unknown"
            if rows[0][1]:
                return json.loads(rows[0][0]), "reused"
            db.execute("UPDATE refresh SET spent = 1 WHERE digest = ?", (digest,))
            return json.loads(rows[0][0]), "ok"
        return self._atomically(spend)

    def purge(self, now):
        for table in ("codes", "access", "refresh"):
            self._run(f"DELETE FROM {table} WHERE exp <= ?", (now,))

    def close(self) -> None:
        self._db.close()


__all__ = ["OAuthStore", "MemoryOAuthStore", "SqliteOAuthStore"]
