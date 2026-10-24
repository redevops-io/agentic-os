"""The assessment ledger: assessments, reviews and exceptions, append-only and hash-chained.

  * An assessment is never edited. A new assessment of the same control, scope and period SUPERSEDES the old one; both stay.
  * A review needs a person who is neither the control's owner nor the producer of any of its evidence (separation of duties). A FAIL cannot be approved:
    the only ways forward are a remediation (a new, passing assessment) or an exception, which does not change the result, only records who accepted the risk,
    until when and what the remediation is.
  * An exception expires. An expired exception is reported as such and no longer excuses anything.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .assess import FAIL, INCONCLUSIVE, MANUAL_REVIEW, NOT_TESTED, PASS, Assessment
from .vault import GENESIS, _canon, _sha


class ReviewError(Exception):
    pass


class Ledger:
    def __init__(self, path: str, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT UNIQUE NOT NULL, "
                      "tenant TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL)")
            for op in ("UPDATE", "DELETE"):
                c.execute(f"CREATE TRIGGER IF NOT EXISTS entries_no_{op.lower()} BEFORE {op} ON entries BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        return c

    def _append(self, kind: str, tenant: str, body: dict) -> str:
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            prev = c.execute("SELECT row_hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["row_hash"] if prev else GENESIS
            at = self.clock()
            ref = f"{kind[:3]}-" + _sha(_canon({"kind": kind, "tenant": tenant, "body": body, "at": at, "prev": prev_hash}))[:20]
            row = {"kind": kind, "ref": ref, "tenant": tenant, "body": _canon(body), "at": at}
            c.execute("INSERT INTO entries (kind, ref, tenant, body, at, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?)",
                      (kind, ref, tenant, row["body"], at, prev_hash, _sha(prev_hash + _canon(row))))
            c.execute("COMMIT")
            return ref
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def _rows(self, kind: str, tenant: str) -> list:
        with self._conn() as c:
            return [(r["ref"], json.loads(r["body"]), r["at"]) for r in c.execute("SELECT * FROM entries WHERE kind=? AND tenant=? ORDER BY seq", (kind, tenant))]

    def verify(self) -> dict:
        problems, prev = [], GENESIS
        with self._conn() as c:
            for r in c.execute("SELECT * FROM entries ORDER BY seq"):
                row = {"kind": r["kind"], "ref": r["ref"], "tenant": r["tenant"], "body": r["body"], "at": r["at"]}
                if r["prev_hash"] != prev or _sha(prev + _canon(row)) != r["row_hash"]:
                    problems.append(f"ledger chain broken at seq {r['seq']}")
                prev = r["row_hash"]
        return {"ok": not problems, "problems": problems, "head": prev}

    # ── assessments ──
    def record_assessment(self, a: Assessment) -> str:
        prior = self.latest(a.tenant, a.control_id, a.scope, a.period_from, a.period_to)
        body = a.to_dict()
        if prior is not None:
            body["supersedes"] = prior[0]
        return self._append("assessment", a.tenant, body)

    def _all_assessments(self) -> list:
        with self._conn() as c:
            return [(r["ref"], json.loads(r["body"]), r["at"]) for r in c.execute("SELECT * FROM entries WHERE kind='assessment' ORDER BY seq")]

    def assessments(self, tenant: str) -> list:
        return self._rows("assessment", tenant)

    def latest(self, tenant: str, control_id: str, scope: str, period_from: float, period_to: float) -> Optional[tuple]:
        match = [r for r in self.assessments(tenant) if r[1]["control_id"] == control_id and r[1]["scope"] == scope
                 and r[1]["period_from"] == period_from and r[1]["period_to"] == period_to]
        return match[-1] if match else None

    def history(self, tenant: str, control_id: str, scope: str) -> list:
        return [r for r in self.assessments(tenant) if r[1]["control_id"] == control_id and r[1]["scope"] == scope]

    # ── review (separation of duties) ──
    def review(self, assessment_ref: str, tenant: str, *, reviewer: str, decision: str, notes: str, control_owner: str, producers: tuple = ()) -> str:
        a = next((r for r in self.assessments(tenant) if r[0] == assessment_ref), None)
        if a is None:
            raise ReviewError("no such assessment for this tenant")
        if decision not in ("approve", "reject", "needs_info"):
            raise ReviewError("decision must be approve, reject or needs_info")
        if not reviewer.strip() or len(notes.strip()) < 10:
            raise ReviewError("the reviewer must be named and give notes")
        if reviewer == control_owner:
            raise ReviewError("the control owner cannot review their own control")
        if reviewer in producers:
            raise ReviewError("a producer of the evidence cannot review it")
        if decision == "approve" and a[1]["result"] in (FAIL, INCONCLUSIVE, NOT_TESTED):
            raise ReviewError(f"a {a[1]['result']} assessment cannot be approved: remediate and reassess, or record an exception")
        return self._append("review", tenant, {"assessment": assessment_ref, "reviewer": reviewer.strip(), "decision": decision, "notes": notes.strip()[:1000]})

    def reviews_of(self, tenant: str, assessment_ref: str) -> list:
        return [r for r in self._rows("review", tenant) if r[1]["assessment"] == assessment_ref]

    # ── exceptions ──
    def open_exception(self, tenant: str, *, control_id: str, scope: str, owner: str, accepted_by: str, reason: str, expires_at: float, remediation: str) -> str:
        if not owner.strip() or not accepted_by.strip() or owner == accepted_by:
            raise ReviewError("an exception needs an owner and a different person accepting the risk")
        if len(reason.strip()) < 15 or len(remediation.strip()) < 15:
            raise ReviewError("an exception needs a reason and a remediation plan")
        if expires_at <= self.clock():
            raise ReviewError("an exception must expire in the future")
        return self._append("exception", tenant, {"control_id": control_id, "scope": scope, "owner": owner.strip(), "accepted_by": accepted_by.strip(),
                                                  "reason": reason.strip(), "expires_at": expires_at, "remediation": remediation.strip()})

    def close_exception(self, tenant: str, exception_ref: str, *, by: str, remediation_evidence: tuple) -> str:
        if not by.strip() or not remediation_evidence:
            raise ReviewError("closing an exception needs a named person and remediation evidence")
        return self._append("exception_closed", tenant, {"exception": exception_ref, "by": by.strip(), "evidence": list(remediation_evidence)})

    def exceptions(self, tenant: str, control_id: str = "", scope: str = "") -> list:
        closed = {r[1]["exception"] for r in self._rows("exception_closed", tenant)}
        now = self.clock()
        out = []
        for ref, b, at in self._rows("exception", tenant):
            if control_id and b["control_id"] != control_id or scope and b["scope"] != scope:
                continue
            out.append({"ref": ref, **b, "state": "closed" if ref in closed else ("expired" if b["expires_at"] <= now else "open")})
        return out

    def status(self, tenant: str, assessment_ref: str) -> dict:
        """The assessment's result, whether a reviewer accepted it, and any exception: the result itself is never changed by either."""
        a = next((r for r in self.assessments(tenant) if r[0] == assessment_ref), None)
        if a is None:
            raise ReviewError("no such assessment for this tenant")
        revs = self.reviews_of(tenant, assessment_ref)
        excs = self.exceptions(tenant, a[1]["control_id"], a[1]["scope"])
        newer = [r for r in self.history(tenant, a[1]["control_id"], a[1]["scope"]) if r[2] > a[2] and r[0] != assessment_ref and r[1]["period_to"] >= a[1]["period_to"]]
        return {"result": a[1]["result"], "reviewed": any(r[1]["decision"] == "approve" for r in revs), "rejected": any(r[1]["decision"] == "reject" for r in revs),
                "superseded_by": newer[-1][0] if newer else "", "exceptions": excs,
                "open_exception": any(e["state"] == "open" for e in excs), "expired_exception": any(e["state"] == "expired" for e in excs)}
