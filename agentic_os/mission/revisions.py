"""Revision-aware approvals: an approval authorizes ONE immutable revision of ONE artifact for ONE action, exactly once.

The problem. A human approves "publish this draft". If the draft is then revised, the approval must not carry over to
the new content, must not stay open as a second, stale decision, and must not be usable twice or by racing the revision.
The per-node approval marker and the signed approval token (``approval.py``) bind the *inputs* a node is about to run
with; they have no notion of an artifact that is revised over time, so a newer revision neither superseded an older
pending approval nor invalidated it explicitly, and nothing made "approval still valid" and "execute" one atomic step.

The model (generic: nothing here knows about any one app).
  * ``artifact_key``  identifies a lineage of revisions, e.g. ``"social/campaign-7/instagram"``. It is chosen by the app.
  * a **revision** is ``(artifact_key, rev, digest)``: ``rev`` counts 1, 2, 3...; ``digest`` is the content digest. Revisions
    are immutable and append-only; ``register_revision`` creates N+1 (or returns the head when the digest is unchanged).
  * an **approval** binds ``(artifact_key, rev, digest, action, target, tenant, principal)`` and moves through
    REQUESTED -> GRANTED -> CONSUMED, or REJECTED / EXPIRED / SUPERSEDED. CONSUMED, REJECTED, EXPIRED and SUPERSEDED are final.
  * creating revision N+1 **supersedes** every open approval (REQUESTED, and by default GRANTED) of revisions <= N in the SAME
    transaction as the new revision, recording the successor and the reason. Nothing is deleted.
  * **consume** is the execution gate: in one transaction it checks the binding, the status, expiry and that the revision is
    still current, and flips GRANTED -> CONSUMED with a compare-and-swap. Two racers, a replay, or a revision created
    in between cannot both succeed; a refusal is recorded as an event and nothing executes.
  * every state change appends an immutable, hash-chained event (requested, granted, rejected, superseded, consumed,
    expired, rejected_stale, replay, binding_mismatch ...). SQL triggers refuse UPDATE/DELETE on the event table, and
    ``verify_chain`` detects tampering. ``export`` returns the full history for audit.

Storage is SQLite (stdlib) in WAL mode with ``BEGIN IMMEDIATE`` transactions, so the guarantees hold across threads AND
processes on one host. For several hosts, the same interface needs a transactional shared store (Postgres); the SQL here is
deliberately plain. See docs/REVISION_AWARE_APPROVALS.md for the lifecycle, event schema, failure semantics and threat model.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

REQUESTED, GRANTED, CONSUMED, REJECTED, EXPIRED, SUPERSEDED = "REQUESTED", "GRANTED", "CONSUMED", "REJECTED", "EXPIRED", "SUPERSEDED"
OPEN_STATES = (REQUESTED, GRANTED)
FINAL_STATES = (CONSUMED, REJECTED, EXPIRED, SUPERSEDED)

# machine-readable refusal codes (the event type for a refusal is the same string, prefixed "rejected_" only for stale)
STALE_REVISION = "stale_revision"
SUPERSEDED_CODE = "superseded"
REPLAY = "replay"
NOT_GRANTED = "not_granted"
NOT_PENDING = "not_pending"
EXPIRED_CODE = "expired"
BINDING_MISMATCH = "binding_mismatch"
TENANT_MISMATCH = "tenant_mismatch"
UNKNOWN_APPROVAL = "unknown_approval"
UNKNOWN_ARTIFACT = "unknown_artifact"
DIGEST_MISMATCH = "digest_mismatch"
TARGET_NOT_IN_REVISION = "target_not_in_revision"
REVISION_CONFLICT = "revision_conflict"
SELF_APPROVAL = "self_approval"

_UNSET = object()


class RevisionError(Exception):
    """A refusal. ``code`` is machine-readable and is also what was written to the audit trail."""

    def __init__(self, code: str, detail: str = "", **info: Any):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.info = code, detail, info


class ApprovalRefused(RevisionError):
    """consume/grant/request was refused; nothing executed or changed (except the audit event)."""


class RevisionConflict(RevisionError):
    """register_revision(expected_parent=...) lost an optimistic-concurrency race."""


@dataclass(frozen=True)
class Revision:
    artifact_key: str
    rev: int
    digest: str
    parent_rev: int | None
    actor: str
    tenant: str
    created_at: float
    meta: dict = field(default_factory=dict)
    superseded: tuple = ()            # approval ids superseded by creating this revision (only on the registering call)
    created: bool = True              # False when register_revision found the head already had this digest


@dataclass(frozen=True)
class Approval:
    approval_id: str
    artifact_key: str
    rev: int
    digest: str
    action: str
    target: str
    tenant: str
    principal: str
    status: str
    mission_id: str
    node_id: str
    requested_at: float
    expires_at: float | None
    decided_by: str
    decided_at: float | None
    superseded_by_rev: int | None
    superseded_reason: str
    consumed_at: float | None
    consumed_by: str


@dataclass(frozen=True)
class RevisionPolicy:
    """How strictly approvals follow the artifact's head.

    ``supersede_granted`` (default True, the safe choice): creating a new revision supersedes GRANTED approvals of older
    revisions too, so an approval can only ever execute the CURRENT revision. Set False only for an action where executing
    the exact, immutable revision a human approved is acceptable after the artifact moved on: GRANTED approvals then survive
    (and can execute their own revision), while REQUESTED ones are still superseded (a human must not decide on stale content).
    """
    supersede_granted: bool = True
    forbid_self_approval: bool = False       # approver == principal (who asked) is refused when True


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_key TEXT PRIMARY KEY, tenant TEXT NOT NULL, head_rev INTEGER NOT NULL, head_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (
  artifact_key TEXT NOT NULL, rev INTEGER NOT NULL, digest TEXT NOT NULL, parent_rev INTEGER, actor TEXT NOT NULL,
  tenant TEXT NOT NULL, created_at REAL NOT NULL, meta TEXT NOT NULL, PRIMARY KEY (artifact_key, rev));
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, artifact_key TEXT NOT NULL, rev INTEGER NOT NULL, digest TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL, tenant TEXT NOT NULL, principal TEXT NOT NULL, status TEXT NOT NULL, mission_id TEXT NOT NULL,
  node_id TEXT NOT NULL, requested_at REAL NOT NULL, expires_at REAL, decided_by TEXT NOT NULL DEFAULT '', decided_at REAL,
  superseded_by_rev INTEGER, superseded_reason TEXT NOT NULL DEFAULT '', consumed_at REAL, consumed_by TEXT NOT NULL DEFAULT '',
  reconciled INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS approvals_by_artifact ON approvals (artifact_key, rev, status);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, type TEXT NOT NULL, artifact_key TEXT NOT NULL, approval_id TEXT NOT NULL,
  rev INTEGER, digest TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}', prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_by_artifact ON events (artifact_key, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'approval history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'approval history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS revisions_no_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS revisions_no_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END;
"""

_GENESIS = "0" * 64
_APPROVAL_COLS = ("approval_id, artifact_key, rev, digest, action, target, tenant, principal, status, mission_id, node_id, requested_at, "
                  "expires_at, decided_by, decided_at, superseded_by_rev, superseded_reason, consumed_at, consumed_by")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def content_digest(value: Any) -> str:
    """sha256 over canonical JSON (sorted keys, no whitespace); strings and bytes are hashed as they are."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode()
    else:
        raw = _canon(value).encode()
    return hashlib.sha256(raw).hexdigest()


def _new_id() -> str:
    return "apr-" + os.urandom(10).hex()


def _row_to_approval(r: tuple) -> Approval:
    return Approval(*r)


class RevisionLedger:
    """Durable, transactional store of artifact revisions, approvals and their append-only history."""

    def __init__(self, path: str | None = None, *, policy: RevisionPolicy | None = None, busy_timeout_s: float = 30.0):
        self.policy = policy or RevisionPolicy()
        if path is None:
            self._tmp = tempfile.mkdtemp(prefix="agentic-os-revisions-")
            path = os.path.join(self._tmp, "ledger.db")
        self.path, self._busy = path, busy_timeout_s
        self._init_lock = threading.Lock()
        with self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ── plumbing ────────────────────────────────────────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=self._busy, isolation_level=None)
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextlib.contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction: the write lock is taken up front, so a check and the write it guards cannot interleave with
        another writer (thread or process). Commits on normal exit (including after a recorded refusal), rolls back on error."""
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")
        finally:
            c.close()

    @staticmethod
    def _emit(c: sqlite3.Connection, type_: str, artifact_key: str, approval_id: str = "", *, rev: int | None = None, digest: str = "",
              actor: str = "", reason: str = "", detail: dict | None = None, now: float) -> None:
        row = c.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row[0] if row else _GENESIS
        body = _canon({"ts": now, "type": type_, "artifact_key": artifact_key, "approval_id": approval_id, "rev": rev, "digest": digest,
                       "actor": actor, "reason": reason, "detail": detail or {}})
        h = hashlib.sha256((prev + body).encode()).hexdigest()
        c.execute("INSERT INTO events (ts, type, artifact_key, approval_id, rev, digest, actor, reason, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (now, type_, artifact_key, approval_id, rev, digest, actor, reason, _canon(detail or {}), prev, h))

    def _refuse(self, c: sqlite3.Connection, code: str, artifact_key: str, approval_id: str, *, rev: int | None, digest: str, actor: str, detail: str, now: float,
                info: dict | None = None, event: str | None = None) -> "ApprovalRefused":
        """Record the refusal (it commits with the transaction) and return the exception for the caller to raise after the commit."""
        self._emit(c, event or f"rejected_{code}", artifact_key, approval_id, rev=rev, digest=digest, actor=actor, reason=code, detail={"detail": detail, **(info or {})}, now=now)
        return ApprovalRefused(code, detail, **(info or {}))

    # ── revisions ───────────────────────────────────────────────────────────────────────────────────────────────
    def head(self, artifact_key: str) -> Revision | None:
        with self._connect() as c:
            r = c.execute("SELECT r.artifact_key, r.rev, r.digest, r.parent_rev, r.actor, r.tenant, r.created_at, r.meta FROM artifacts a JOIN revisions r "
                          "ON r.artifact_key=a.artifact_key AND r.rev=a.head_rev WHERE a.artifact_key=?", (artifact_key,)).fetchone()
        return None if r is None else Revision(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7]), created=False)

    def revisions(self, artifact_key: str) -> list[Revision]:
        with self._connect() as c:
            rows = c.execute("SELECT artifact_key, rev, digest, parent_rev, actor, tenant, created_at, meta FROM revisions WHERE artifact_key=? ORDER BY rev", (artifact_key,)).fetchall()
        return [Revision(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7]), created=False) for r in rows]

    def register_revision(self, artifact_key: str, digest: str, *, actor: str, tenant: str, meta: dict | None = None,
                          expected_parent: Any = _UNSET, now: float | None = None) -> Revision:
        """Create revision N+1 (or revision 1) with ``digest``, atomically superseding every open approval it makes stale.

        Idempotent: if the head already has this digest, return it with ``created=False`` and change nothing.
        ``expected_parent`` (an int, or None meaning "must be the first revision") makes the call an optimistic-concurrency
        compare-and-swap: a mismatch raises :class:`RevisionConflict` and records a ``revision_conflict`` event, so two writers
        revising the same draft cannot both believe they extended the same parent."""
        if not artifact_key or not digest or not actor or not tenant:
            raise RevisionError("invalid", "artifact_key, digest, actor and tenant are required")
        t = time.time() if now is None else now
        conflict: RevisionConflict | None = None
        with self._txn() as c:
            row = c.execute("SELECT tenant, head_rev, head_digest FROM artifacts WHERE artifact_key=?", (artifact_key,)).fetchone()
            if row is not None and row[0] != tenant:
                self._emit(c, "rejected_tenant_mismatch", artifact_key, rev=row[1], actor=actor, reason=TENANT_MISMATCH, detail={"claimed_tenant": tenant}, now=t)
                raise_after = ApprovalRefused(TENANT_MISMATCH, "artifact belongs to another tenant")
            else:
                raise_after = None
                head_rev = row[1] if row else None
                if row is not None and row[2] == digest:                       # unchanged content is not a new revision
                    out = c.execute("SELECT artifact_key, rev, digest, parent_rev, actor, tenant, created_at, meta FROM revisions WHERE artifact_key=? AND rev=?", (artifact_key, head_rev)).fetchone()
                    return Revision(out[0], out[1], out[2], out[3], out[4], out[5], out[6], json.loads(out[7]), created=False)
                if expected_parent is not _UNSET and expected_parent != head_rev:
                    self._emit(c, "revision_conflict", artifact_key, rev=head_rev, digest=digest, actor=actor, reason=REVISION_CONFLICT,
                               detail={"expected_parent": expected_parent, "head": head_rev}, now=t)
                    conflict = RevisionConflict(REVISION_CONFLICT, f"expected parent {expected_parent!r}, head is {head_rev!r}", expected_parent=expected_parent, head=head_rev)
                else:
                    new_rev = (head_rev or 0) + 1
                    c.execute("INSERT INTO revisions (artifact_key, rev, digest, parent_rev, actor, tenant, created_at, meta) VALUES (?,?,?,?,?,?,?,?)",
                              (artifact_key, new_rev, digest, head_rev, actor, tenant, t, _canon(meta or {})))
                    if row is None:
                        c.execute("INSERT INTO artifacts (artifact_key, tenant, head_rev, head_digest) VALUES (?,?,?,?)", (artifact_key, tenant, new_rev, digest))
                    else:
                        c.execute("UPDATE artifacts SET head_rev=?, head_digest=? WHERE artifact_key=?", (new_rev, digest, artifact_key))
                    self._emit(c, "revision_created", artifact_key, rev=new_rev, digest=digest, actor=actor, detail={"parent_rev": head_rev, "meta_keys": sorted((meta or {}))}, now=t)
                    states = OPEN_STATES if self.policy.supersede_granted else (REQUESTED,)
                    ph = ",".join("?" * len(states))
                    old = c.execute(f"SELECT approval_id, rev, digest, status FROM approvals WHERE artifact_key=? AND rev<? AND status IN ({ph})", (artifact_key, new_rev, *states)).fetchall()
                    sup = []
                    for aid, orev, odig, ost in old:
                        c.execute("UPDATE approvals SET status=?, superseded_by_rev=?, superseded_reason=? WHERE approval_id=? AND status=?",
                                  (SUPERSEDED, new_rev, f"artifact revised: {orev} -> {new_rev}", aid, ost))
                        self._emit(c, "superseded", artifact_key, aid, rev=orev, digest=odig, actor=actor, reason=f"revision {new_rev} created",
                                   detail={"was": ost, "superseded_by_rev": new_rev, "successor_digest": digest}, now=t)
                        sup.append(aid)
                    return Revision(artifact_key, new_rev, digest, head_rev, actor, tenant, t, dict(meta or {}), superseded=tuple(sup), created=True)
        if raise_after is not None:
            raise raise_after
        assert conflict is not None
        raise conflict

    # ── approvals ───────────────────────────────────────────────────────────────────────────────────────────────
    def request_approval(self, *, artifact_key: str, rev: int, digest: str, action: str, target: str = "", tenant: str, principal: str,
                         mission_id: str = "", node_id: str = "", ttl_s: float = 0.0, now: float | None = None) -> Approval:
        """Open an approval bound to exactly (artifact_key, rev, digest, action, target, tenant, principal). Refused if ``rev`` is not the head."""
        t = time.time() if now is None else now
        err: ApprovalRefused | None = None
        with self._txn() as c:
            h = c.execute("SELECT tenant, head_rev, head_digest FROM artifacts WHERE artifact_key=?", (artifact_key,)).fetchone()
            if h is None:
                err = self._refuse(c, UNKNOWN_ARTIFACT, artifact_key, "", rev=rev, digest=digest, actor=principal, detail="no such artifact", now=t)
            elif h[0] != tenant:
                err = self._refuse(c, TENANT_MISMATCH, artifact_key, "", rev=rev, digest=digest, actor=principal, detail="artifact belongs to another tenant", now=t)
            elif h[1] != rev:
                err = self._refuse(c, STALE_REVISION, artifact_key, "", rev=rev, digest=digest, actor=principal, detail=f"revision {rev} is not current (head {h[1]})",
                                   now=t, info={"head_rev": h[1]}, event="rejected_stale")
            elif h[2] != digest:
                err = self._refuse(c, DIGEST_MISMATCH, artifact_key, "", rev=rev, digest=digest, actor=principal, detail="digest is not the head revision's", now=t)
            else:
                meta = json.loads(c.execute("SELECT meta FROM revisions WHERE artifact_key=? AND rev=?", (artifact_key, rev)).fetchone()[0])
                if meta.get("target") not in (None, "") and target not in (None, "") and str(meta["target"]) != str(target):
                    err = self._refuse(c, TARGET_NOT_IN_REVISION, artifact_key, "", rev=rev, digest=digest, actor=principal, detail="target is not the one this revision produced", now=t)
                else:
                    if mission_id and node_id:                       # re-parking the same gate must not open a second approval for it
                        dup = c.execute("SELECT approval_id FROM approvals WHERE artifact_key=? AND rev=? AND action=? AND target=? AND tenant=? AND mission_id=? AND node_id=? "
                                        "AND status IN (?,?) ORDER BY requested_at LIMIT 1", (artifact_key, rev, action, target or "", tenant, mission_id, node_id, REQUESTED, GRANTED)).fetchone()
                        if dup is not None:
                            return self._approval(c, dup[0])
                    aid = _new_id()
                    c.execute("INSERT INTO approvals (approval_id, artifact_key, rev, digest, action, target, tenant, principal, status, mission_id, node_id, requested_at, expires_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (aid, artifact_key, rev, digest, action, target or "", tenant, principal, REQUESTED, mission_id, node_id, t, (t + ttl_s) if ttl_s else None))
                    self._emit(c, "requested", artifact_key, aid, rev=rev, digest=digest, actor=principal,
                               detail={"action": action, "target": target, "tenant": tenant, "mission_id": mission_id, "node_id": node_id, "expires_at": (t + ttl_s) if ttl_s else None}, now=t)
                    return self._approval(c, aid)
        assert err is not None
        raise err

    def grant(self, approval_id: str, *, approver: str, now: float | None = None) -> Approval:
        """A human decision: REQUESTED -> GRANTED. Refused (and recorded) if the approval was superseded, expired, already decided, or its revision is no longer the head."""
        t = time.time() if now is None else now
        err: ApprovalRefused | None = None
        with self._txn() as c:
            a = self._approval(c, approval_id)
            if a is None:
                err = self._refuse(c, UNKNOWN_APPROVAL, "", approval_id, rev=None, digest="", actor=approver, detail="no such approval", now=t)
            elif a.status == SUPERSEDED:
                err = self._refuse(c, SUPERSEDED_CODE, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, detail=a.superseded_reason, now=t,
                                   info={"superseded_by_rev": a.superseded_by_rev}, event="rejected_stale")
            elif a.status != REQUESTED:
                err = self._refuse(c, NOT_PENDING, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, detail=f"approval is {a.status}", now=t)
            elif a.expires_at is not None and t >= a.expires_at:
                c.execute("UPDATE approvals SET status=?, decided_at=? WHERE approval_id=? AND status=?", (EXPIRED, t, approval_id, REQUESTED))
                self._emit(c, "expired", a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, reason=EXPIRED_CODE, detail={"expires_at": a.expires_at}, now=t)
                err = ApprovalRefused(EXPIRED_CODE, "approval expired before it was granted")
            elif self.policy.forbid_self_approval and approver == a.principal:
                err = self._refuse(c, SELF_APPROVAL, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, detail="the requester cannot approve", now=t)
            else:
                h = c.execute("SELECT head_rev, head_digest FROM artifacts WHERE artifact_key=?", (a.artifact_key,)).fetchone()
                if h is None or h[0] != a.rev or h[1] != a.digest:         # defensive: supersession should already have caught this
                    err = self._refuse(c, STALE_REVISION, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, detail="revision is no longer current", now=t, event="rejected_stale")
                else:
                    c.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE approval_id=? AND status=?", (GRANTED, approver, t, approval_id, REQUESTED))
                    self._emit(c, "granted", a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, now=t)
                    return self._approval(c, approval_id)
        assert err is not None
        raise err

    def reject(self, approval_id: str, *, approver: str, reason: str = "", now: float | None = None) -> Approval:
        t = time.time() if now is None else now
        err: ApprovalRefused | None = None
        with self._txn() as c:
            a = self._approval(c, approval_id)
            if a is None:
                err = self._refuse(c, UNKNOWN_APPROVAL, "", approval_id, rev=None, digest="", actor=approver, detail="no such approval", now=t)
            elif a.status != REQUESTED:
                err = self._refuse(c, NOT_PENDING, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, detail=f"approval is {a.status}", now=t)
            else:
                c.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE approval_id=? AND status=?", (REJECTED, approver, t, approval_id, REQUESTED))
                self._emit(c, "rejected", a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=approver, reason=reason, now=t)
                return self._approval(c, approval_id)
        assert err is not None
        raise err

    def consume(self, approval_id: str, *, artifact_key: str, rev: int | None = None, digest: str = "", action: str, target: str = "", tenant: str,
                executor: str = "", now: float | None = None) -> Approval:
        """THE execution gate. In ONE transaction: the approval exists and is GRANTED; its binding equals what is about to run
        (artifact, action, target, tenant, and the revision/digest if the caller states them); it has not expired; the revision is still
        the head (unless the policy lets a GRANTED approval execute its own immutable revision); then GRANTED -> CONSUMED by
        compare-and-swap. Anything else raises :class:`ApprovalRefused`, records why, and the caller must not execute."""
        t = time.time() if now is None else now
        err: ApprovalRefused | None = None
        with self._txn() as c:
            a = self._approval(c, approval_id)
            rid = rev if rev is not None else None
            if a is None:
                err = self._refuse(c, UNKNOWN_APPROVAL, artifact_key, approval_id, rev=rid, digest=digest, actor=executor, detail="no such approval", now=t)
            elif a.tenant != tenant:
                err = self._refuse(c, TENANT_MISMATCH, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail="approval belongs to another tenant", now=t)
            else:
                bad = [name for name, want, have in (("artifact_key", artifact_key, a.artifact_key), ("action", action, a.action), ("target", target or "", a.target))
                       if want != have]
                if rev is not None and rev != a.rev:
                    bad.append("rev")
                if digest and digest != a.digest:
                    bad.append("digest")
                if bad:
                    err = self._refuse(c, BINDING_MISMATCH, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail=f"does not match the approval: {', '.join(bad)}",
                                       now=t, info={"fields": bad})
                elif a.status == CONSUMED:
                    err = self._refuse(c, REPLAY, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail="approval was already used", now=t, event="replay")
                elif a.status == SUPERSEDED:
                    err = self._refuse(c, SUPERSEDED_CODE, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail=a.superseded_reason, now=t,
                                       info={"superseded_by_rev": a.superseded_by_rev}, event="rejected_stale")
                elif a.status != GRANTED:
                    err = self._refuse(c, NOT_GRANTED, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail=f"approval is {a.status}, not GRANTED", now=t)
                elif a.expires_at is not None and t >= a.expires_at:
                    c.execute("UPDATE approvals SET status=? WHERE approval_id=? AND status=?", (EXPIRED, approval_id, GRANTED))
                    self._emit(c, "expired", a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, reason=EXPIRED_CODE, detail={"expires_at": a.expires_at}, now=t)
                    err = ApprovalRefused(EXPIRED_CODE, "approval expired before it was used")
                else:
                    h = c.execute("SELECT head_rev, head_digest FROM artifacts WHERE artifact_key=?", (a.artifact_key,)).fetchone()
                    if self.policy.supersede_granted and (h is None or h[0] != a.rev or h[1] != a.digest):
                        err = self._refuse(c, STALE_REVISION, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail=f"revision {a.rev} is not current (head {h[0] if h else None})",
                                           now=t, info={"head_rev": h[0] if h else None}, event="rejected_stale")
                    else:
                        cur = c.execute("UPDATE approvals SET status=?, consumed_at=?, consumed_by=? WHERE approval_id=? AND status=?", (CONSUMED, t, executor, approval_id, GRANTED))
                        if cur.rowcount != 1:                                  # lost a race to another consumer inside the same instant
                            err = self._refuse(c, REPLAY, a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail="approval was already used", now=t, event="replay")
                        else:
                            self._emit(c, "consumed", a.artifact_key, approval_id, rev=a.rev, digest=a.digest, actor=executor, detail={"action": a.action, "target": a.target}, now=t)
                            return self._approval(c, approval_id)
        assert err is not None
        raise err

    def expire_due(self, *, now: float | None = None) -> list[str]:
        """Mark open approvals whose ttl has passed as EXPIRED (a sweep; grant/consume also enforce expiry on their own)."""
        t = time.time() if now is None else now
        out = []
        with self._txn() as c:
            for aid, key, rev, dig in c.execute("SELECT approval_id, artifact_key, rev, digest FROM approvals WHERE status IN (?,?) AND expires_at IS NOT NULL AND expires_at<=?", (REQUESTED, GRANTED, t)).fetchall():
                c.execute("UPDATE approvals SET status=? WHERE approval_id=?", (EXPIRED, aid))
                self._emit(c, "expired", key, aid, rev=rev, digest=dig, reason=EXPIRED_CODE, now=t)
                out.append(aid)
        return out

    # ── reads ───────────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _approval(c: sqlite3.Connection, approval_id: str) -> Approval | None:
        r = c.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return None if r is None else _row_to_approval(r)

    def approval(self, approval_id: str) -> Approval | None:
        with self._connect() as c:
            return self._approval(c, approval_id)

    def approvals(self, artifact_key: str, *, status: str | None = None) -> list[Approval]:
        with self._connect() as c:
            q, args = f"SELECT {_APPROVAL_COLS} FROM approvals WHERE artifact_key=?", [artifact_key]
            if status:
                q, args = q + " AND status=?", args + [status]
            return [_row_to_approval(r) for r in c.execute(q + " ORDER BY requested_at, approval_id", args).fetchall()]

    def unreconciled_supersessions(self) -> list[Approval]:
        """SUPERSEDED approvals whose mission has not yet been told (the runtime drains this and calls ``mark_reconciled``)."""
        with self._connect() as c:
            return [_row_to_approval(r) for r in c.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE status=? AND reconciled=0 AND mission_id<>'' ORDER BY requested_at", (SUPERSEDED,)).fetchall()]

    def approvals_for_mission(self, mission_id: str) -> list[Approval]:
        with self._connect() as c:
            return [_row_to_approval(r) for r in c.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE mission_id=? ORDER BY requested_at", (mission_id,)).fetchall()]

    def mark_reconciled(self, approval_id: str) -> None:
        with self._txn() as c:
            c.execute("UPDATE approvals SET reconciled=1 WHERE approval_id=?", (approval_id,))

    def history(self, artifact_key: str | None = None, *, approval_id: str | None = None) -> list[dict]:
        q, args = "SELECT seq, ts, type, artifact_key, approval_id, rev, digest, actor, reason, detail, prev_hash, hash FROM events", []
        where = []
        if artifact_key is not None:
            where.append("artifact_key=?"); args.append(artifact_key)
        if approval_id is not None:
            where.append("approval_id=?"); args.append(approval_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        with self._connect() as c:
            rows = c.execute(q + " ORDER BY seq", args).fetchall()
        return [{"seq": r[0], "ts": r[1], "type": r[2], "artifact_key": r[3], "approval_id": r[4], "rev": r[5], "digest": r[6], "actor": r[7], "reason": r[8],
                 "detail": json.loads(r[9]), "prev_hash": r[10], "hash": r[11]} for r in rows]

    def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute the hash chain over ALL events. (True, None) when intact, else (False, seq of the first broken event)."""
        prev = _GENESIS
        with self._connect() as c:
            for seq, ts, type_, key, aid, rev, dig, actor, reason, detail, ph, h in c.execute(
                    "SELECT seq, ts, type, artifact_key, approval_id, rev, digest, actor, reason, detail, prev_hash, hash FROM events ORDER BY seq"):
                body = _canon({"ts": ts, "type": type_, "artifact_key": key, "approval_id": aid, "rev": rev, "digest": dig, "actor": actor, "reason": reason, "detail": json.loads(detail)})
                if ph != prev or hashlib.sha256((prev + body).encode()).hexdigest() != h:
                    return False, seq
                prev = h
        return True, None

    def export(self, artifact_key: str) -> dict:
        """The complete, self-contained audit record of one artifact: revisions, approvals (every state, including superseded) and every event."""
        ok, broken = self.verify_chain()
        return {"artifact_key": artifact_key, "chain_intact": ok, "first_broken_seq": broken,
                "revisions": [r.__dict__ | {"superseded": list(r.superseded)} for r in self.revisions(artifact_key)],
                "approvals": [a.__dict__ for a in self.approvals(artifact_key)], "events": self.history(artifact_key)}


def revision_ledger_from_env(env: dict | None = None) -> "RevisionLedger | None":
    """``AGENTIC_OS_REVISION_LEDGER=<path to a sqlite file>`` -> a ledger; unset -> None (revision-bound capabilities then fail closed)."""
    e = os.environ if env is None else env
    p = (e.get("AGENTIC_OS_REVISION_LEDGER") or "").strip()
    return RevisionLedger(p) if p else None
