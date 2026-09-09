"""Real read-only **PostgreSQL** context source.

This is the vertical slice that proves what local files cannot: *structured* evidence,
secure credentials, read-only schema discovery, schema/table scoping, freshness, and
read-only governance. It plugs into the same :class:`~agentic_os.sources.SourceConnector`
seam as :class:`~agentic_os.sources.LocalFilesConnector`.

Decoupled from the driver via a **DB-API 2.0 connection seam** (:data:`DbConnect`): tests
drive a fake connection returning canned ``information_schema`` rows; production lazily uses
``psycopg`` (v3) or ``psycopg2``. Two governance invariants hold regardless of driver:

* the session is put in **read-only** mode on connect, and
* every query goes through :func:`_select`, which refuses anything but ``SELECT`` / ``WITH`` —
  the connector *cannot* issue a write even if the code around it changes.

The credential (user/password) is resolved from a ``CredentialRef`` at the moment of use and
never stored on the :class:`~agentic_os.sources.ContextSource` or in an
:class:`~agentic_os.sources.EvidenceRef`.

**Two evidence shapes, and two separate concerns.**

* :func:`catalog_evidence` = **catalog evidence** — schema *identity* (which tables/columns
  exist, one ref per table). Provenance, not content.
* :func:`retrieve_records` = **query evidence** — the actual records supporting a Mission,
  retrieved through the path a Mission consumes (a scoped, read-only, identifier-validated
  ``SELECT``), returned as a :class:`RetrievedEvidence` with a record count and observed-at.

And this connector is deliberately only **source connectivity** — identify, authorize, scope,
observe, and *retrieve*. It is NOT **context materialization/indexing** (deciding what to
ingest/cache and in which representation — BM25 / vector / graph). That is Context Runtime's
physical-plan choice, reached through the ``Indexer`` seam; for many Missions the right plan
for a live database is *scoped SQL against the source* (this retrieve path), not ingesting the
whole database into RAG — whereas PDFs from a drive are better indexed. Keeping the two apart
is what lets Context Runtime own that decision per task.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from runtime_contracts.canonical import content_hash

from .sources import (
    AccessMode,
    ContextSource,
    CountingIndexer,
    EvidenceRef,
    Indexer,
    ProposedSource,
    SourceGrant,
    SourceHealth,
    SourceHealthState,
    SourceKind,
)

#: A DB-API 2.0 connection factory: connection params → a live connection.
DbConnect = Callable[[Dict[str, Any]], Any]

# Discovery is read-only by construction — a single information_schema projection.
_CATALOG_SQL = (
    "SELECT table_schema, table_name, column_name, data_type "
    "FROM information_schema.columns "
    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
    "ORDER BY table_schema, table_name, ordinal_position"
)
_READ_ONLY_SQL = "SET default_transaction_read_only = on"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    columns: Tuple[Tuple[str, str], ...]  # (column, type)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


class ReadOnlyViolation(Exception):
    """A non-read query was attempted through the read-only connector."""


def _select(conn: Any, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
    """Run a read query and return all rows. Refuses anything but SELECT/WITH — the hard
    governance guard so this connector can never mutate a source."""
    head = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
    if head not in ("select", "with"):
        raise ReadOnlyViolation(f"read-only connector refused a non-SELECT query: {head!r}")
    cur = conn.cursor()
    try:
        cur.execute(sql, tuple(params))
        try:
            return list(cur.fetchall())
        except Exception:  # a statement with no result set
            return []
    finally:
        cur.close()


def _enforce_read_only(conn: Any) -> None:
    cur = conn.cursor()
    try:
        cur.execute(_READ_ONLY_SQL)
    finally:
        cur.close()


def _denied(qualified: str, schema: str, denied: Sequence[str]) -> bool:
    """A denied entry can name a table (``billing.card_data``), a schema wildcard (``hr.*``),
    or a bare schema (``hr``)."""
    for d in denied:
        d = d.strip()
        if not d:
            continue
        if fnmatch.fnmatch(qualified, d) or d == schema or fnmatch.fnmatch(schema, d):
            return True
    return False


def discover_catalog(conn: Any, *, allowed_schemas: Sequence[str] = (),
                     allowed_tables: Sequence[str] = (), denied: Sequence[str] = ()) -> List[TableInfo]:
    """Read-only schema discovery, scoped. ``allowed_schemas``/``allowed_tables`` are
    allow-lists (empty = all user schemas / all tables); ``denied`` carves out sub-scopes."""
    allow_s = {s for s in allowed_schemas if s}
    allow_t = {t for t in allowed_tables if t}
    grouped: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    order: List[Tuple[str, str]] = []
    for schema, table, column, dtype in _select(conn, _CATALOG_SQL):
        if allow_s and schema not in allow_s:
            continue
        qualified = f"{schema}.{table}"
        if allow_t and qualified not in allow_t and table not in allow_t:
            continue
        if _denied(qualified, schema, denied):
            continue
        key = (schema, table)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append((column, dtype))
    return [TableInfo(schema=s, name=t, columns=tuple(grouped[(s, t)])) for (s, t) in order]


def catalog_evidence(source_id: str, catalog: Sequence[TableInfo]) -> List[EvidenceRef]:
    """CATALOG evidence — schema identity. One EvidenceRef per table (``kind="table"``),
    addressable and summarised, with no row content and no secret. This is *what exists*, not
    *what supported a Mission*."""
    return [
        EvidenceRef(source_id=source_id, ref=f"postgres:{t.qualified}", kind="table",
                    summary=f"{t.qualified} ({len(t.columns)} columns: "
                            f"{', '.join(c for c, _ in t.columns[:6])}{'…' if len(t.columns) > 6 else ''})")
        for t in catalog
    ]


@dataclass(frozen=True)
class RetrievedEvidence:
    """QUERY evidence — the actual records supporting a Mission, distinct from catalog
    evidence (schema identity). Carries provenance (the exact query, how many rows, when
    observed) so Projects' "Context used" can say "14 records retrieved · observed 10:41";
    the rows are addressable :class:`EvidenceRef` (``kind="record"``), never dumped inline,
    never a secret."""

    source_id: str
    table: str
    query: str
    record_count: int
    observed_at: str
    refs: Tuple[EvidenceRef, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {"source_id": self.source_id, "table": self.table, "query": self.query,
                "record_count": self.record_count, "observed_at": self.observed_at,
                "refs": [r.to_dict() for r in self.refs]}


def _ident(name: str) -> str:
    # A double-quoted SQL identifier with embedded quotes escaped — used ONLY on names we've
    # already validated against the live catalog, so this is defence in depth, not the gate.
    return '"' + name.replace('"', '""') + '"'


def _row_summary(columns: Sequence[str], row: Sequence[Any]) -> str:
    return " · ".join(f"{c}={row[i]}" for i, c in enumerate(columns[:3]) if i < len(row))


def retrieve_records(conn: Any, table: TableInfo, *, source_id: str, columns: Sequence[str] = (),
                     where_column: str = "", where_value: Any = None, limit: int = 50,
                     clock: Callable[[], str] = _now) -> RetrievedEvidence:
    """QUERY evidence: retrieve real rows from an *already-discovered, in-scope* table via a
    scoped read-only SELECT, and return them as EvidenceRefs a Mission consumes.

    Injection-safe by construction: column and filter identifiers are accepted only if they
    appear in ``table.columns`` (the identifiers we discovered), the filter *value* is bound
    as a parameter, a ``LIMIT`` is always applied, and the whole thing runs through
    :func:`_select` (read-only). Passing an unknown column is ignored, not interpolated.
    """
    known = {c for c, _ in table.columns}
    cols = [c for c in columns if c in known] or [c for c, _ in table.columns]
    col_sql = ", ".join(_ident(c) for c in cols)
    sql = f"SELECT {col_sql} FROM {_ident(table.schema)}.{_ident(table.name)}"
    params: Tuple[Any, ...] = ()
    if where_column and where_column in known:
        sql += f" WHERE {_ident(where_column)} = %s"
        params = (where_value,)
    sql += f" LIMIT {int(limit)}"
    rows = _select(conn, sql, params)
    refs = tuple(
        EvidenceRef(source_id=source_id, ref=f"postgres:{table.qualified}#{i}", kind="record",
                    summary=_row_summary(cols, r))
        for i, r in enumerate(rows)
    )
    return RetrievedEvidence(source_id=source_id, table=table.qualified, query=sql,
                             record_count=len(rows), observed_at=clock(), refs=refs)


def psycopg_connect(params: Dict[str, Any]) -> Any:  # pragma: no cover — needs a live DB + driver
    """Default factory: lazily open a connection with psycopg (v3) or psycopg2."""
    kwargs = {k: params[k] for k in ("host", "port", "dbname", "user", "password") if params.get(k)}
    try:
        import psycopg  # type: ignore
        conn = psycopg.connect(**kwargs)
    except ImportError:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(**kwargs)
    try:
        conn.autocommit = True
    except Exception:
        pass
    return conn


@dataclass
class PostgresSourceConnector:
    """A read-only PostgreSQL SourceConnector. ``resolver`` resolves the credential ref to
    ``{"user", "password"}`` (and optionally ``host``/``port``/``dbname`` overrides);
    ``connect`` is the DB-API seam (defaults to :func:`psycopg_connect`)."""

    resolver: Any
    connect: DbConnect = psycopg_connect
    indexer: Indexer = field(default_factory=CountingIndexer)
    clock: Callable[[], str] = _now
    kind: SourceKind = SourceKind.DATABASE

    def _params(self, spec: ProposedSource, credential_ref: str) -> Dict[str, Any]:
        # location is "host/dbname" or "host:port/dbname"; credential fills user/password.
        host, _, dbname = spec.location.partition("/")
        port: Optional[int] = None
        if ":" in host:
            host, _, p = host.partition(":")
            port = int(p) if p.isdigit() else None
        material = dict(self.resolver.resolve(credential_ref)) if credential_ref else {}
        params: Dict[str, Any] = {"host": host or "localhost", "dbname": dbname or material.get("dbname", "")}
        if port:
            params["port"] = port
        for k in ("user", "password", "port", "dbname", "host"):
            if material.get(k):
                params[k] = material[k]
        return params

    def connect_and_scan(self, spec: ProposedSource, *, project_id: str, source_id: str,
                         credential_ref: str = "") -> ContextSource:
        grant = SourceGrant(allowed_schemas=tuple(spec.allowed_schemas),
                            access_mode=AccessMode.READ_ONLY)  # governance: read-only, always
        now = self.clock()
        try:
            conn = self.connect(self._params(spec, credential_ref))
        except Exception as e:  # connection/credential failure — surfaced, never raised through
            return self._error_source(spec, project_id, source_id, credential_ref, grant, now,
                                      f"connection failed: {type(e).__name__}")
        try:
            _enforce_read_only(conn)
            catalog = discover_catalog(conn, allowed_schemas=spec.allowed_schemas,
                                       denied=grant.denied)
        except Exception as e:
            return self._error_source(spec, project_id, source_id, credential_ref, grant, now,
                                      f"discovery failed: {type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        schemas = sorted({t.schema for t in catalog})
        columns = sum(len(t.columns) for t in catalog)
        indexed = self.indexer.index(project_id, source_id, [t.qualified for t in catalog]) \
            if spec.indexing_policy.value == "automatic" else 0
        fingerprint = content_hash({"catalog": [[t.schema, t.name, list(t.columns)] for t in catalog]})
        return ContextSource(
            source_id=source_id, project_id=project_id, name=spec.display_name() or (dbname_of(spec) or "postgres"),
            kind=SourceKind.DATABASE, location=spec.location, provider="postgres",
            credential_ref=credential_ref, grant=grant, indexing_policy=spec.indexing_policy,
            health=SourceHealth(SourceHealthState.HEALTHY,
                                f"read-only · {len(catalog)} tables in {len(schemas)} schemas", now),
            stats={"schemas": len(schemas), "tables": len(catalog), "columns": columns, "indexed": indexed},
            last_observed_at=now, source_fingerprint=fingerprint,
        )

    def _error_source(self, spec, project_id, source_id, credential_ref, grant, now, detail) -> ContextSource:
        return ContextSource(
            source_id=source_id, project_id=project_id, name=spec.display_name() or "postgres",
            kind=SourceKind.DATABASE, location=spec.location, provider="postgres",
            credential_ref=credential_ref, grant=grant, indexing_policy=spec.indexing_policy,
            health=SourceHealth(SourceHealthState.ERROR, detail, now),
            stats={"schemas": 0, "tables": 0, "columns": 0}, last_observed_at=now,
        )


def dbname_of(spec: ProposedSource) -> str:
    return spec.location.partition("/")[2]
