"""Durable, queryable observation store — Postgres as the operational truth for the real-data loop
(REAL_DATA_PROVIDER_SCOPING.md v2 §6 / PR-sequence.odt §6, PR 1).

JSONL proved the mechanism; the first LIVE loop needs relational, concurrent, as-of-queryable storage,
so canonical observations land in **Postgres** (a deployment with webhooks/workers writing concurrently
wants a real database, not another append-only file). This store persists the bi-temporal
:class:`~agentic_os.observation.Observation` — ``valid_at`` (world truth), ``known_at`` (legitimately
knowable → replay correctness), ``ingested_at`` (operational), and ``known_at_quality`` (A0
admissibility) — and answers the leakage-safe **as-of** query in SQL.

``psycopg`` (v3) is imported lazily, so this module (and the package) import cleanly where Postgres
isn't installed; it's an optional runtime dependency of deployments that use this store. The connection
string comes from the environment (``OBS_DATABASE_URL``), never hard-coded.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence

from agentic_os.observation import KnownAtQuality, Observation

_A0_QUALITIES = ("observed", "reconstructed")   # UNKNOWN is excluded fail-closed (see historical_replay)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  observation_id    text PRIMARY KEY,
  source            text NOT NULL,
  kind              text NOT NULL,
  subject           text NOT NULL,
  valid_at          double precision NOT NULL,
  known_at          double precision NOT NULL,
  ingested_at       double precision NOT NULL,
  known_at_quality  text NOT NULL,
  payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence_refs     text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_obs_subject ON observations(subject);
CREATE INDEX IF NOT EXISTS ix_obs_asof ON observations(known_at, valid_at);
"""


def observation_dsn(dsn: Optional[str] = None) -> Optional[str]:
    """The operational Postgres DSN: the arg, else ``$OBS_DATABASE_URL`` (or ``$DATABASE_URL``)."""
    return dsn or os.environ.get("OBS_DATABASE_URL") or os.environ.get("DATABASE_URL") or None


class PostgresObservationStore:
    """Append-only, as-of-queryable observation store backed by Postgres. Idempotent on
    ``observation_id`` (re-delivered webhooks are safe). Construct with a DSN or from the environment."""

    def __init__(self, dsn: Optional[str] = None, *, ensure_schema: bool = True) -> None:
        resolved = observation_dsn(dsn)
        if not resolved:
            raise ValueError("no Postgres DSN (pass dsn= or set OBS_DATABASE_URL)")
        import psycopg  # lazy: keeps psycopg optional for the rest of the package
        self._psycopg = psycopg
        self._conn = psycopg.connect(resolved, autocommit=True)
        if ensure_schema:
            with self._conn.cursor() as cur:
                cur.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def append(self, obs: Observation) -> None:
        from psycopg.types.json import Jsonb
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO observations
                     (observation_id, source, kind, subject, valid_at, known_at, ingested_at,
                      known_at_quality, payload, evidence_refs)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (observation_id) DO NOTHING""",
                (obs.observation_id, obs.source, obs.kind, obs.subject, obs.valid_at, obs.known_at,
                 obs.ingested_at, obs.known_at_quality.value, Jsonb(dict(obs.payload)),
                 list(obs.evidence_refs)))

    def get(self, observation_id: str) -> Optional[Observation]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT observation_id, source, kind, subject, valid_at, known_at, ingested_at, "
                        "known_at_quality, payload, evidence_refs FROM observations "
                        "WHERE observation_id = %s", (observation_id,))
            row = cur.fetchone()
        return _row_to_obs(row) if row else None

    def as_of(self, decision_time: float, *, subject: Optional[str] = None,
              require_quality: bool = True) -> List[Observation]:
        """The leakage-safe reconstruction in SQL: observations knowable at ``decision_time``
        (``valid_at <= T AND known_at <= T``), optionally one subject, and — with ``require_quality``
        (the A0 default) — only OBSERVED/RECONSTRUCTED provenance (UNKNOWN fails closed)."""
        clauses = ["valid_at <= %(t)s", "known_at <= %(t)s"]
        params: dict = {"t": decision_time}
        if subject is not None:
            clauses.append("subject = %(subject)s"); params["subject"] = subject
        if require_quality:
            clauses.append("known_at_quality = ANY(%(q)s)"); params["q"] = list(_A0_QUALITIES)
        sql = ("SELECT observation_id, source, kind, subject, valid_at, known_at, ingested_at, "
               "known_at_quality, payload, evidence_refs FROM observations WHERE "
               + " AND ".join(clauses) + " ORDER BY valid_at, observation_id")
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_row_to_obs(r) for r in rows]

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM observations")
            return int(cur.fetchone()[0])


def _row_to_obs(row) -> Observation:
    (oid, source, kind, subject, valid_at, known_at, ingested_at, quality, payload, refs) = row
    if isinstance(payload, str):                 # some drivers hand back jsonb as text
        payload = json.loads(payload)
    return Observation(
        observation_id=oid, source=source, kind=kind, subject=subject,
        valid_at=float(valid_at), known_at=float(known_at),
        payload=dict(payload or {}), evidence_refs=tuple(refs or ()),
        ingested_at=float(ingested_at), known_at_quality=KnownAtQuality(quality))
