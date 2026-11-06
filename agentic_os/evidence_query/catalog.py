"""The scoped artifact catalog.

A *scope* is the unit of authorization (a project, a mission group, a tenant, the built-in synthetic corpus). Runtime
evidence datasets are registered per scope, either as rows already loaded by the runtime or as a confined artifact file
with its recorded sha256. A worker is built from ONE scope's datasets only (``load_scope``), after authorization, so a
query can never name, see or time another scope's data: it is not in the process.

Views are operator-defined SELECTs over the scope's own datasets (validated with the same parser-based guard); they are
the "authorized views" through which derived or masked projections are exposed. Column projection (``DatasetSpec.columns``)
is the AGPL-core masking primitive; row/column/document-level policy per user is the Enterprise seam (see docs)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .artifacts import ArtifactFile
from .errors import ArtifactError, IntegrityError
from .guard import StatementRejected, check_statement
from .limits import Limits

SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")          # no dots: permission names are evidence.<scope>.<action>
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
TYPES = {"VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "BOOLEAN", "TIMESTAMP", "DATE", "JSON"}
RESERVED = {"main", "memory", "system", "temp", "information_schema", "pg_catalog"}


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    columns: Tuple[Tuple[str, str], ...]
    rows: Optional[Sequence[dict]] = None
    artifact: Optional[ArtifactFile] = None

    def __post_init__(self):
        if not NAME_RE.match(self.name) or self.name in RESERVED or self.name.startswith("duckdb_"):
            raise ValueError("invalid dataset name")
        if not self.columns:
            raise ValueError("a dataset needs columns")
        seen = set()
        for c, t in self.columns:
            if not NAME_RE.match(c) or c in seen or str(t).upper() not in TYPES:
                raise ValueError("invalid column definition")
            seen.add(c)
        if (self.rows is None) == (self.artifact is None):
            raise ValueError("exactly one of rows / artifact")


@dataclass(frozen=True)
class ViewSpec:
    name: str
    sql: str

    def __post_init__(self):
        if not NAME_RE.match(self.name) or self.name in RESERVED:
            raise ValueError("invalid view name")


@dataclass
class LoadedDataset:
    name: str
    columns: Tuple[Tuple[str, str], ...]
    rows: List[dict]
    sha256: str


@dataclass
class ScopeData:
    scope_id: str
    datasets: List[LoadedDataset]
    views: List[ViewSpec] = field(default_factory=list)

    def hashes(self) -> Dict[str, str]:
        return {d.name: d.sha256 for d in self.datasets}

    def table_names(self) -> set:
        return {d.name for d in self.datasets} | {v.name for v in self.views}

    def request(self, sql: str, limits: Limits) -> dict:
        return {"mode": "query", "sql": sql,
                "datasets": [{"name": d.name, "columns": [list(c) for c in d.columns], "rows": d.rows} for d in self.datasets],
                "views": [{"name": v.name, "sql": v.sql} for v in self.views],
                "limits": {"timeout_s": limits.timeout_s, "memory_mb": limits.memory_mb, "max_rows": limits.max_rows,
                           "max_result_bytes": limits.max_result_bytes, "max_sql_chars": limits.max_sql_chars,
                           "threads": limits.threads}}

    def request_repr(self) -> str:
        return json.dumps(self.request("", Limits()), default=str)


def _norm(value: Any, typ: str) -> Any:
    if value is None:
        return None
    t = typ.upper()
    if t in ("JSON", "VARCHAR") and not isinstance(value, str):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def rows_sha256(rows: Sequence[dict]) -> str:
    return hashlib.sha256(json.dumps(list(rows), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class ScopeCatalog:
    def __init__(self) -> None:
        self._scopes: Dict[str, Tuple[Tuple[DatasetSpec, ...], Tuple[ViewSpec, ...]]] = {}

    def register_scope(self, scope_id: str, datasets: Sequence[DatasetSpec], views: Sequence[ViewSpec] = ()) -> None:
        if not isinstance(scope_id, str) or not SCOPE_RE.match(scope_id):
            raise ValueError("invalid scope id")
        names = [d.name for d in datasets] + [v.name for v in views]
        if len(set(names)) != len(names):
            raise ValueError("duplicate table or view name in scope")
        for v in views:                                    # views may only read the scope's own tables
            try:
                check_statement(v.sql, set(names) - {v.name})
            except StatementRejected:
                raise ValueError("view definition is not a valid read-only SELECT over the scope's datasets") from None
        self._scopes[scope_id] = (tuple(datasets), tuple(views))

    def has_scope(self, scope_id: str) -> bool:
        return scope_id in self._scopes

    def scope_ids(self) -> List[str]:
        return sorted(self._scopes)

    def table_names(self, scope_id: str) -> set:
        ds, vs = self._scopes[scope_id]
        return {d.name for d in ds} | {v.name for v in vs}

    def load_scope(self, scope_id: str) -> ScopeData:
        """Build the worker input for ONE scope. Artifact bytes are hash-verified here (IntegrityError on mismatch)."""
        ds, vs = self._scopes[scope_id]
        loaded: List[LoadedDataset] = []
        for d in ds:
            if d.artifact is not None:
                rows, digest = d.artifact.load()
            else:
                rows, digest = list(d.rows), rows_sha256(d.rows)
            cols = tuple((c, str(t).upper()) for c, t in d.columns)   # column projection: only declared columns leave
            loaded.append(LoadedDataset(d.name, cols, [{c: _norm(r.get(c), t) for c, t in cols} for r in rows], digest))
        return ScopeData(scope_id, loaded, list(vs))

    def verify_scope(self, scope_id: str) -> Dict[str, bool]:
        ds, _ = self._scopes[scope_id]
        out = {}
        for d in ds:
            out[d.name] = d.artifact.verify() if d.artifact is not None else True
        return out
