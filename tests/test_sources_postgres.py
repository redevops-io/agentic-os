"""Read-only PostgreSQL source connector, driven by a fake DB-API connection (no live DB,
no driver). Proves schema discovery, schema/table scoping + denied, read-only governance,
freshness fingerprint, structured EvidenceRefs, and that the credential never leaks."""
from __future__ import annotations

import json

import pytest

from agentic_os.sources import ProposedSource, SourceHealthState, SourceKind
from agentic_os.sources_postgres import (
    PostgresSourceConnector,
    ReadOnlyViolation,
    RetrievedEvidence,
    TableInfo,
    _select,
    catalog_evidence,
    discover_catalog,
    retrieve_records,
)

# Real rows support.tickets would return (columns id, subject, status).
TICKET_ROWS = [(1, "Billed twice", "open"), (2, "Refund please", "open")]

# A fake credential following the repo's .gitleaks.toml convention (the "-DO-NOT-STORE"
# marker) — used only to prove it is resolved-at-use and never lands on the source/evidence.
FAKE_PW = "fake-pw-DO-NOT-STORE"

# (table_schema, table_name, column_name, data_type) as information_schema.columns returns.
CATALOG_ROWS = [
    ("public", "customers", "id", "integer"),
    ("public", "customers", "email", "text"),
    ("support", "tickets", "id", "integer"),
    ("support", "tickets", "subject", "text"),
    ("support", "tickets", "status", "text"),
    ("billing", "card_data", "pan", "text"),   # sensitive — must be deniable
    ("hr", "salaries", "amount", "numeric"),   # sensitive schema — must be deniable
]


class FakeCursor:
    def __init__(self, conn: "FakeConn"):
        self._conn = conn
        self._rows: list = []

    def execute(self, sql, params=()):
        self._conn.executed.append(sql.strip())
        self._conn.params.append(tuple(params))
        if "information_schema.columns" in sql:
            self._rows = CATALOG_ROWS
        elif "tickets" in sql:
            self._rows = TICKET_ROWS
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.executed: list = []
        self.params: list = []
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class Resolver:
    def resolve(self, ref):
        return {"user": "svc_readonly", "password": FAKE_PW}


def _connector(fail=False):
    def connect(_params):
        if fail:
            raise ConnectionError("could not connect")
        return FakeConn()
    return PostgresSourceConnector(resolver=Resolver(), connect=connect, clock=lambda: "2026-09-09T00:00:00Z")


def test_discovery_groups_columns_per_table():
    catalog = discover_catalog(FakeConn())
    tickets = next(t for t in catalog if t.qualified == "support.tickets")
    assert [c for c, _ in tickets.columns] == ["id", "subject", "status"]


def test_schema_allowlist_and_denied_scoping():
    catalog = discover_catalog(FakeConn(), allowed_schemas=["public", "support", "billing", "hr"],
                               denied=["billing.card_data", "hr.*"])
    quals = {t.qualified for t in catalog}
    assert "support.tickets" in quals
    assert "billing.card_data" not in quals  # denied by exact table
    assert not any(t.schema == "hr" for t in catalog)  # denied by schema wildcard


def test_allowed_schemas_restricts():
    catalog = discover_catalog(FakeConn(), allowed_schemas=["support"])
    assert {t.schema for t in catalog} == {"support"}


def test_select_guard_refuses_writes():
    conn = FakeConn()
    with pytest.raises(ReadOnlyViolation):
        _select(conn, "UPDATE customers SET email='x'")
    with pytest.raises(ReadOnlyViolation):
        _select(conn, "DELETE FROM tickets")
    assert _select(conn, "SELECT 1") == []  # a real SELECT is allowed


def test_connect_and_scan_enforces_read_only_and_reports_catalog():
    spec = ProposedSource(kind=SourceKind.DATABASE, location="localhost:5432/customer_ops",
                          allowed_schemas=["public", "support"])
    cs = _connector().connect_and_scan(spec, project_id="p1", source_id="src-db",
                                       credential_ref="crm:cred")
    assert cs.kind is SourceKind.DATABASE and cs.provider == "postgres"
    assert cs.health.state is SourceHealthState.HEALTHY
    assert cs.stats["schemas"] == 2 and cs.stats["tables"] == 2  # public.customers + support.tickets
    assert cs.grant.access_mode.value == "read_only"  # governance: always read-only
    assert cs.source_fingerprint  # freshness is content-addressed over the catalog


def test_read_only_session_is_set_before_discovery():
    spec = ProposedSource(kind=SourceKind.DATABASE, location="localhost/db")
    conn_box = {}

    def connect(_p):
        c = FakeConn(); conn_box["c"] = c; return c
    PostgresSourceConnector(resolver=Resolver(), connect=connect,
                            clock=lambda: "t").connect_and_scan(spec, project_id="p", source_id="s")
    executed = conn_box["c"].executed
    read_only_at = next(i for i, e in enumerate(executed) if "read_only = on" in e)
    discovery_at = next(i for i, e in enumerate(executed) if "information_schema.columns" in e)
    assert read_only_at < discovery_at  # read-only is enforced before any discovery query runs


def test_connection_failure_is_error_health_not_a_crash():
    spec = ProposedSource(kind=SourceKind.DATABASE, location="localhost/db")
    cs = _connector(fail=True).connect_and_scan(spec, project_id="p", source_id="s", credential_ref="c")
    assert cs.health.state is SourceHealthState.ERROR and "connection failed" in cs.health.detail


def test_evidence_refs_are_structured_and_leak_no_secret():
    catalog = discover_catalog(FakeConn(), allowed_schemas=["support"])
    refs = catalog_evidence("src-db", catalog)
    assert refs[0].ref == "postgres:support.tickets" and refs[0].kind == "table"
    blob = json.dumps([r.to_dict() for r in refs])
    assert FAKE_PW not in blob and "password" not in blob


def test_credential_never_appears_on_the_source():
    spec = ProposedSource(kind=SourceKind.DATABASE, location="localhost/db", allowed_schemas=["support"])
    cs = _connector().connect_and_scan(spec, project_id="p", source_id="s", credential_ref="crm:cred")
    assert FAKE_PW not in json.dumps(cs.to_projection())


# ── query evidence: the actual records supporting a Mission (vs catalog identity) ──
TICKETS = TableInfo(schema="support", name="tickets",
                    columns=(("id", "integer"), ("subject", "text"), ("status", "text")))


def test_retrieve_records_returns_query_evidence():
    ev = retrieve_records(FakeConn(), TICKETS, source_id="src-db", limit=10, clock=lambda: "2026-09-09T10:41:00Z")
    assert isinstance(ev, RetrievedEvidence)
    assert ev.record_count == 2 and ev.observed_at == "2026-09-09T10:41:00Z"
    assert ev.query.lower().startswith("select") and ev.table == "support.tickets"
    assert [r.kind for r in ev.refs] == ["record", "record"]  # rows, not schema identity
    assert ev.refs[0].ref == "postgres:support.tickets#0"
    assert "subject=Billed twice" in ev.refs[0].summary


def test_retrieve_records_ignores_unknown_columns_and_never_interpolates_them():
    ev = retrieve_records(FakeConn(), TICKETS, source_id="s", columns=["id", "subject", "OR 1=1; DROP"])
    assert '"id"' in ev.query and '"subject"' in ev.query
    assert "DROP" not in ev.query  # unknown identifier dropped, not interpolated


def test_retrieve_records_binds_the_filter_value_as_a_parameter():
    conn = FakeConn()
    ev = retrieve_records(conn, TICKETS, source_id="s", where_column="status", where_value="open")
    assert 'WHERE "status" = %s' in ev.query   # value is a bound param, not string-built
    assert ("open",) in conn.params            # the value went through parameter binding


def test_retrieve_records_is_read_only():
    # A record query is still a SELECT through the read-only guard — writes are impossible.
    conn = FakeConn()
    retrieve_records(conn, TICKETS, source_id="s")
    assert all(not e.lstrip().lower().startswith(("update", "delete", "insert")) for e in conn.executed)


def test_catalog_vs_query_evidence_are_distinct_kinds():
    catalog = discover_catalog(FakeConn(), allowed_schemas=["support"])
    cat_refs = catalog_evidence("s", catalog)
    qry = retrieve_records(FakeConn(), TICKETS, source_id="s")
    assert {r.kind for r in cat_refs} == {"table"}       # what exists
    assert {r.kind for r in qry.refs} == {"record"}      # what supported the Mission
