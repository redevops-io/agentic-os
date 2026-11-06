"""Statement guard - DEFENCE IN DEPTH, never the boundary.

The boundary is the sandboxed worker process (no external access, locked configuration, no inherited secrets, data
passed in as already-loaded rows). This guard exists to (1) give the caller a clean, fixed refusal instead of an
engine error, (2) keep DDL/DML/PRAGMA/SET out of the engine even though the worker could not persist them, and (3) hide
catalog introspection. It uses DuckDB's own parser (statement-type introspection and ``json_serialize_sql``), not
regular expressions, and fails closed: anything the parser cannot serialise is rejected.

Stdlib + duckdb only: the worker imports this file directly (it does not import the rest of the package)."""
from __future__ import annotations

import json

MAX_DEPTH = 200

# Table functions a statement may call. Everything else (read_*, glob, query, duckdb_*, pragma_*, *_scan ...) is out.
TABLE_FUNCTION_ALLOW = frozenset({"range", "generate_series", "unnest", "json_each", "json_tree", "generate_subscripts"})

# Parse-tree node types that are catalog/introspection forms (DESCRIBE, SUMMARIZE, SHOW ...).
DENIED_NODE_TYPES = frozenset({"SHOW_REF", "COLUMN_DATA", "DELIM_GET"})

# Scalar functions that reach settings, the environment, the filesystem or the catalog.
FUNCTION_DENY = frozenset({
    "current_setting", "getenv", "getvariable", "setvariable", "query", "query_table", "glob", "which_secret",
    "sniff_csv", "load_extension", "write_log", "current_database", "current_catalog", "current_schema",
    "current_schemas", "version", "pg_get_viewdef", "pg_get_constraintdef", "txid_current", "checkpoint",
    "force_checkpoint", "pragma_version", "parquet_metadata", "parquet_schema",
})
FUNCTION_DENY_PREFIX = ("read_", "duckdb_", "pragma_", "parquet_", "sqlite_", "iceberg_", "delta_", "http_", "httpfs_",
                        "glob", "icu_", "which_", "sniff_", "postgres_", "mysql_", "excel_", "st_read")


class StatementRejected(Exception):
    """Fixed, short, path-free message."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("statement rejected: only a single read-only SELECT over the scope's tables is allowed")


def _walk(node, tables, funcs, table_funcs, ctes, depth=0):
    if depth > MAX_DEPTH:
        raise StatementRejected("too_deep")
    if isinstance(node, dict):
        t = node.get("type")
        if not isinstance(t, str):
            t = None
        if t in DENIED_NODE_TYPES:
            raise StatementRejected("node_type")
        if t == "BASE_TABLE":
            tables.append((node.get("catalog_name") or "", node.get("schema_name") or "", str(node.get("table_name") or "")))
        elif t == "TABLE_FUNCTION":
            fn = node.get("function") or {}
            table_funcs.append(str(fn.get("function_name") or ""))
        if "function_name" in node and t != "BASE_TABLE":
            funcs.append((str(node.get("function_name") or ""), node.get("schema") or ""))
        cm = node.get("cte_map")
        if isinstance(cm, dict):
            for entry in cm.get("map") or []:
                if isinstance(entry, dict) and "key" in entry:
                    ctes.add(str(entry["key"]).lower())
        for v in node.values():
            _walk(v, tables, funcs, table_funcs, ctes, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, tables, funcs, table_funcs, ctes, depth + 1)


def check_statement(sql: str, allowed_tables, *, max_chars: int = 8000, con=None) -> None:
    """Raise :class:`StatementRejected` unless ``sql`` is exactly one SELECT/WITH statement that touches only
    ``allowed_tables`` (plus its own CTEs), calls only allow-listed table functions and no denied scalar function."""
    import duckdb

    if not isinstance(sql, str) or not sql.strip() or len(sql) > max_chars or "\x00" in sql:
        raise StatementRejected("empty_or_oversized")
    own = con is None
    if own:
        con = duckdb.connect(":memory:", config={"enable_external_access": False, "autoinstall_known_extensions": False,
                                                 "autoload_known_extensions": False})
    try:
        try:
            stmts = con.extract_statements(sql)
        except Exception:                                       # noqa: BLE001 - parse error
            raise StatementRejected("parse_error") from None
        if len(stmts) != 1:
            raise StatementRejected("not_single_statement")
        if str(stmts[0].type) != "StatementType.SELECT":
            raise StatementRejected("not_select")
        try:
            raw = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
            tree = json.loads(raw)
        except Exception:                                       # noqa: BLE001
            raise StatementRejected("not_serialisable") from None
        if tree.get("error"):
            raise StatementRejected("not_serialisable")
        tables, funcs, table_funcs, ctes = [], [], [], set()
        _walk(tree, tables, funcs, table_funcs, ctes)
        allowed = {str(t).lower() for t in allowed_tables}
        for cat, schema, name in tables:
            if cat or schema:
                raise StatementRejected("qualified_table")
            if name.lower() not in allowed and name.lower() not in ctes:
                raise StatementRejected("unknown_table")
        for name in table_funcs:
            if name.lower() not in TABLE_FUNCTION_ALLOW:
                raise StatementRejected("table_function")
        for name, schema in funcs:
            n = name.lower()
            if n in FUNCTION_DENY or n.startswith(FUNCTION_DENY_PREFIX):
                if n in TABLE_FUNCTION_ALLOW and n in table_funcs:
                    continue
                raise StatementRejected("function")
    finally:
        if own:
            con.close()
