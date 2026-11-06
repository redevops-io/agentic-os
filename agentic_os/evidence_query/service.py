"""The Evidence API core: authorize -> resolve scope -> audit -> sandboxed read-only execution.

Order matters and is tested: the permission is checked BEFORE the scope's data is resolved or a worker starts (SQL
filtering is not a boundary); an unknown scope and an unauthorized one are the same refusal; the request is audited on
every path, and a sink that cannot write stops the request."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import worker as W
from .artifacts import ArtifactError
from .audit import AuditSink, new_receipt, statement_sha256
from .authz import Ceiling, allowed, perm
from .catalog import ScopeCatalog
from .errors import IntegrityError, QueryRefused
from .guard import StatementRejected, check_statement
from .limits import Limits


@dataclass
class QueryResult:
    columns: List[str]
    rows: List[list]
    truncated: bool
    receipt_id: str
    dataset_hashes: Dict[str, str] = field(default_factory=dict)


class EvidenceQueryService:
    def __init__(self, catalog: ScopeCatalog, ceiling: Ceiling, audit: AuditSink, *, limits: Limits = Limits(),
                 os_sandbox: str = "auto"):
        self.catalog = catalog
        self.ceiling = ceiling
        self.audit = audit
        self.limits = limits
        self.os_sandbox = os_sandbox

    # the single place a receipt is written; failure to write is a failure of the request
    def _record(self, receipt: dict) -> dict:
        try:
            return self.audit.append(receipt)
        except Exception:                                  # noqa: BLE001
            raise QueryRefused("audit_unavailable") from None

    def _refuse(self, code: str, reason: str, principal, scope: str, sql: str, t0: float) -> QueryRefused:
        who = getattr(principal, "id", "") if principal is not None else ""
        self._record(new_receipt("evidence.query", who, scope, statement_sha256=statement_sha256(sql or ""),
                                 statement_chars=len(sql or ""), rows=0, outcome="refused", reason=reason,
                                 duration_ms=int((time.time() - t0) * 1000), datasets={}))
        return QueryRefused(code)

    def query(self, principal: Any, scope_id: str, sql: str) -> QueryResult:
        t0 = time.time()
        if principal is None:
            raise self._refuse("unauthenticated", "unauthenticated", None, str(scope_id), sql, t0)
        try:
            permission = perm(scope_id, "inspect")
        except ValueError:
            raise self._refuse("not_found", "unknown_scope", principal, str(scope_id)[:64], sql, t0) from None
        known = self.catalog.has_scope(scope_id)
        ok = allowed(self.ceiling, principal, permission)
        if not known or not ok:
            raise self._refuse("not_found", "unknown_scope" if not known else "not_authorized", principal, scope_id, sql, t0)
        if not isinstance(sql, str) or len(sql) > self.limits.max_sql_chars:
            raise self._refuse("statement_rejected", "statement_rejected", principal, scope_id, sql if isinstance(sql, str) else "", t0)
        try:
            check_statement(sql, self.catalog.table_names(scope_id), max_chars=self.limits.max_sql_chars)
        except StatementRejected:
            raise self._refuse("statement_rejected", "statement_rejected", principal, scope_id, sql, t0) from None
        try:
            data = self.catalog.load_scope(scope_id)       # only after authorization; only this scope's artifacts
        except IntegrityError:
            raise self._refuse("integrity", "integrity_failure", principal, scope_id, sql, t0) from None
        except ArtifactError:
            raise self._refuse("error", "artifact_unreadable", principal, scope_id, sql, t0) from None
        rows_in = sum(len(d.rows) for d in data.datasets)
        if rows_in > self.limits.max_input_rows:
            raise self._refuse("limit", "input_too_large", principal, scope_id, sql, t0)
        try:
            res = W.execute(data, sql, self.limits, os_sandbox=self.os_sandbox)
        except W.WorkerError as e:
            code = {"timeout": "timeout", "limit": "limit", "statement_rejected": "statement_rejected"}.get(e.code, "error")
            outcome = "timeout" if e.code == "timeout" else "error"
            who = getattr(principal, "id", "")
            self._record(new_receipt("evidence.query", who, scope_id, statement_sha256=statement_sha256(sql),
                                     statement_chars=len(sql), rows=0, outcome=outcome, reason=e.code,
                                     duration_ms=int((time.time() - t0) * 1000), datasets=data.hashes()))
            if code == "error" and e.args and e.args[0] and e.args[0] != e.code:
                raise QueryRefused("error", f"query failed: {e.args[0]}") from None
            raise QueryRefused(code) from None
        rec = self._record(new_receipt("evidence.query", getattr(principal, "id", ""), scope_id,
                                       statement_sha256=statement_sha256(sql), statement_chars=len(sql),
                                       rows=len(res.rows), truncated=res.truncated, outcome="ok", reason="",
                                       duration_ms=int((time.time() - t0) * 1000), datasets=data.hashes()))
        return QueryResult(res.columns, res.rows, res.truncated, rec["receipt_id"], data.hashes())
