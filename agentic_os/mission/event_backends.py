"""Durable event-store backends — the same append-only ledger, on DuckDB or Postgres.

The base :class:`~.store.EventStore` is in-memory + JSONL: perfect for a single standalone
install, but it is not a shared substrate and not queryable. This module adds two drop-in
backends behind the *same* five-method surface the repositories rely on
(``append`` / ``for_mission`` / ``all`` / ``mission_ids`` / ``subscribe``), so nothing
downstream changes:

* :class:`DuckDBEventStore` — an **embedded single file** (or ``:memory:``). Zero infra, durable
  across restarts, and analytically queryable — the audit log *is* a table. The right default
  for a durable standalone device.
* :class:`PostgresEventStore` — a **shared** log with DB-assigned sequence numbers, so several
  instances can append to and fold the same ledger safely. This is the Member/Hub substrate:
  instance A creates a mission, instance B rehydrates it by fold from the same rows.

Because the runtime is event-sourced, this is also the durable **log/audit storage** — no
separate logging system. Both drivers are optional extras (``agentic-os[duckdb]`` /
``agentic-os[postgres]``); importing a backend without its driver raises a clear message.

Use :func:`open_event_store` (honours ``MISSION_EVENT_BACKEND``) to select one; the default
stays the zero-dependency in-memory/JSONL store, so existing deployments are unchanged.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, List

from .store import Event, EventStore
from .types import to_jsonable

_CREATE_MARKER = "events"


class _SubscriberMixin:
    """Shared in-process fan-out. Cross-instance notification (LISTEN/NOTIFY) is a later add;
    a subscriber never breaks the log."""

    def _init_subs(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self._subscribers.append(cb)

    def _fanout(self, ev: Event) -> None:
        for cb in list(self._subscribers):
            try:
                cb(ev)
            except Exception:      # a subscriber must never break the log
                pass


class DuckDBEventStore(_SubscriberMixin):
    """Append-only ledger in an embedded DuckDB database (a file, or ``:memory:``)."""

    def __init__(self, path: str = ":memory:"):
        try:
            import duckdb
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "DuckDBEventStore needs the 'duckdb' extra: pip install agentic-os[duckdb]") from e
        self._init_subs()
        self._path = path
        self._con = duckdb.connect(path)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "seq BIGINT PRIMARY KEY, ts DOUBLE, type VARCHAR, mission_id VARCHAR, payload JSON)")
        self._con.execute("CREATE SEQUENCE IF NOT EXISTS event_seq START 1")

    def append(self, type: str, mission_id: str, payload: dict[str, Any]) -> Event:
        body = json.dumps(to_jsonable(payload))
        with self._lock:
            (seq,) = self._con.execute("SELECT nextval('event_seq')").fetchone()
            ev = Event(type=type, mission_id=mission_id, payload=json.loads(body), seq=int(seq))
            self._con.execute(
                "INSERT INTO events (seq, ts, type, mission_id, payload) VALUES (?, ?, ?, ?, ?)",
                [ev.seq, ev.ts, ev.type, ev.mission_id, body])
        self._fanout(ev)
        return ev

    def _rows(self, sql: str, args: list | None = None) -> List[Event]:
        with self._lock:
            rows = self._con.execute(sql, args or []).fetchall()
        return [Event(type=t, mission_id=m, payload=json.loads(p), seq=int(s), ts=float(ts))
                for (s, ts, t, m, p) in rows]

    def for_mission(self, mission_id: str) -> list[Event]:
        return self._rows(
            "SELECT seq, ts, type, mission_id, payload FROM events "
            "WHERE mission_id = ? ORDER BY seq", [mission_id])

    def all(self) -> list[Event]:
        return self._rows("SELECT seq, ts, type, mission_id, payload FROM events ORDER BY seq")

    def mission_ids(self) -> list[str]:
        # first-seen order, matching EventStore.mission_ids()
        with self._lock:
            rows = self._con.execute(
                "SELECT mission_id FROM events WHERE mission_id <> '' "
                "GROUP BY mission_id ORDER BY min(seq)").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._con.close()


class PostgresEventStore(_SubscriberMixin):
    """Append-only ledger in Postgres, with a DB-assigned sequence so many instances can share it."""

    def __init__(self, dsn: str):
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "PostgresEventStore needs the 'postgres' extra: pip install agentic-os[postgres]") from e
        self._init_subs()
        self._dsn = dsn
        self._con = psycopg.connect(dsn, autocommit=True)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "seq BIGSERIAL PRIMARY KEY, ts DOUBLE PRECISION, type TEXT, "
            "mission_id TEXT, payload JSONB)")
        self._con.execute("CREATE INDEX IF NOT EXISTS events_mission_idx ON events (mission_id, seq)")

    def append(self, type: str, mission_id: str, payload: dict[str, Any]) -> Event:
        body = json.dumps(to_jsonable(payload))
        with self._lock:
            row = self._con.execute(
                "INSERT INTO events (ts, type, mission_id, payload) "
                "VALUES (EXTRACT(EPOCH FROM now()), %s, %s, %s) RETURNING seq, ts",
                [type, mission_id, body]).fetchone()
        ev = Event(type=type, mission_id=mission_id, payload=json.loads(body),
                   seq=int(row[0]), ts=float(row[1]))
        self._fanout(ev)
        return ev

    def _rows(self, sql: str, args: list | None = None) -> List[Event]:
        with self._lock:
            rows = self._con.execute(sql, args or []).fetchall()
        return [Event(type=t, mission_id=m,
                      payload=p if isinstance(p, dict) else json.loads(p),
                      seq=int(s), ts=float(ts))
                for (s, ts, t, m, p) in rows]

    def for_mission(self, mission_id: str) -> list[Event]:
        return self._rows(
            "SELECT seq, ts, type, mission_id, payload FROM events "
            "WHERE mission_id = %s ORDER BY seq", [mission_id])

    def all(self) -> list[Event]:
        return self._rows("SELECT seq, ts, type, mission_id, payload FROM events ORDER BY seq")

    def mission_ids(self) -> list[str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT mission_id FROM events WHERE mission_id <> '' "
                "GROUP BY mission_id ORDER BY min(seq)").fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._con.close()


def open_event_store(backend: str | None = None, *, path: str | None = None,
                     dsn: str | None = None):
    """Select an event-store backend. Default = the zero-dependency in-memory/JSONL store.

    ``backend`` (or ``MISSION_EVENT_BACKEND``): ``memory`` | ``jsonl`` | ``duckdb`` | ``postgres``.
    Paths/DSNs fall back to ``MISSION_EVENT_PATH`` / ``MISSION_EVENT_DSN`` (and the legacy
    ``MISSION_EVENT_LOG`` for JSONL), so nothing existing changes unless a backend is chosen.
    """
    backend = (backend or os.environ.get("MISSION_EVENT_BACKEND") or "memory").lower()
    if backend == "memory":
        return EventStore()
    if backend == "jsonl":
        return EventStore(path=path or os.environ.get("MISSION_EVENT_PATH")
                          or os.environ.get("MISSION_EVENT_LOG"))
    if backend == "duckdb":
        return DuckDBEventStore(path or os.environ.get("MISSION_EVENT_PATH") or ":memory:")
    if backend == "postgres":
        target = dsn or os.environ.get("MISSION_EVENT_DSN")
        if not target:
            raise ValueError("postgres backend needs a DSN (MISSION_EVENT_DSN)")
        return PostgresEventStore(target)
    raise ValueError(f"unknown event backend: {backend!r}")
