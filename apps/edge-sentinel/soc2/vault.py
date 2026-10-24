"""The evidence vault: durable, append-only, tenant-scoped, producer-authenticated, hash-chained evidence.

`case_store.CaseStore` keeps evidence in process memory; that is right for a replayable investigation and wrong for audit evidence, which must survive a
restart, resist edits, say who produced it and for which tenant, and prove nothing was removed. This vault is that, and it refuses what it cannot vouch for.

What it enforces on INGEST
  * the producer is authenticated: an envelope carries a key id and an HMAC over its header and the payload digest; the key is registered to ONE producer
    and a list of tenants. A forged producer, a wrong tenant, a changed payload or a changed header fails.
  * a nonce is single-use: a replayed envelope is recognised (returns the first receipt) and never stored twice.
  * `known_at` is the vault's clock, not the producer's: late-arriving evidence is visible as late (`late_by_s`) and cannot be back-dated.
  * the coverage window it claims (`coverage_from`..`coverage_to`) must be sane (ordered, not in the future).
What it enforces on STORAGE and READ
  * rows cannot be updated or deleted through SQL (triggers abort), every row carries the hash of the previous row, and `verify_all` recomputes every payload digest
    and the whole chain: an edited payload, a deleted row or a re-ordered log is reported, never passed.
  * a read is tenant-checked, re-verifies the payload digest, and is recorded in the custody log.
Limits, stated plainly: an administrator with write access to the database FILE can rebuild it consistently (a hash chain detects edits, it does not stop a determined
owner of the file; anchor the head hash somewhere the writer cannot reach for that). The producer keys live with the deployment; a stolen key can sign false
evidence until it is revoked. Retention is recorded, not enforced by deletion: nothing is ever deleted here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

GENESIS = "0" * 64


class VaultError(Exception):
    pass


class EvidenceRejected(VaultError):
    """The envelope was refused; nothing was stored."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code


class EvidenceUnavailable(VaultError):
    """The evidence is missing, belongs to another tenant, or fails verification."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _sha(data: "bytes | str") -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceEnvelope:
    """What a producer sends. `payload` is the raw evidence bytes; `signature` covers every other field and the payload digest."""
    tenant: str
    scope: str                          # the system / environment the evidence is about (a control's `applicable_scope`)
    kind: str                           # e.g. change_records, access_review, backup_restore, heartbeat, attestation, scanner_result
    source: str                         # the producing system
    producer: str                       # the identity that produced it (must match the key's registered producer)
    key_id: str
    payload: bytes
    observed_at: float
    coverage_from: float
    coverage_to: float
    nonce: str
    classification: str = "restricted"
    meta: dict = field(default_factory=dict)
    signature: str = ""

    def header(self) -> dict:
        return {"tenant": self.tenant, "scope": self.scope, "kind": self.kind, "source": self.source, "producer": self.producer, "key_id": self.key_id,
                "sha256": _sha(self.payload), "observed_at": self.observed_at, "coverage_from": self.coverage_from, "coverage_to": self.coverage_to,
                "nonce": self.nonce, "classification": self.classification, "meta": self.meta}


def sign_envelope(env: EvidenceEnvelope, secret: bytes) -> EvidenceEnvelope:
    sig = hmac.new(secret, _canon(env.header()).encode(), hashlib.sha256).hexdigest()
    return EvidenceEnvelope(**{**env.__dict__, "signature": sig})


@dataclass
class ProducerKey:
    producer: str
    tenants: tuple                       # the tenants this key may produce evidence for
    secret: bytes
    revoked: bool = False


class ProducerKeys:
    def __init__(self) -> None:
        self._keys: dict = {}

    def register(self, key_id: str, producer: str, tenants, secret: bytes) -> None:
        if not secret or len(secret) < 16:
            raise ValueError("a producer key needs a real secret")
        self._keys[key_id] = ProducerKey(producer, tuple(tenants), secret)

    def revoke(self, key_id: str) -> None:
        if key_id in self._keys:
            self._keys[key_id].revoked = True

    def get(self, key_id: str) -> Optional[ProducerKey]:
        return self._keys.get(key_id)


@dataclass(frozen=True)
class Receipt:
    artifact_id: str
    duplicate: bool
    known_at: float
    late_by_s: float


@dataclass(frozen=True)
class Stored:
    artifact_id: str
    tenant: str
    scope: str
    kind: str
    source: str
    producer: str
    key_id: str
    sha256: str
    size: int
    observed_at: float
    known_at: float
    coverage_from: float
    coverage_to: float
    late_by_s: float
    classification: str
    meta: dict


class EvidenceVault:
    def __init__(self, path: str, keys: ProducerKeys, *, clock: Callable[[], float] = time.time, late_after_s: float = 86_400.0) -> None:
        self.path, self.keys, self.clock, self.late_after_s = path, keys, clock, float(late_after_s)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS evidence (seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT UNIQUE NOT NULL, tenant TEXT NOT NULL, "
                      "scope TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, producer TEXT NOT NULL, key_id TEXT NOT NULL, sha256 TEXT NOT NULL, "
                      "size INTEGER NOT NULL, content BLOB NOT NULL, observed_at REAL NOT NULL, known_at REAL NOT NULL, coverage_from REAL NOT NULL, "
                      "coverage_to REAL NOT NULL, late_by_s REAL NOT NULL, classification TEXT NOT NULL, nonce TEXT UNIQUE NOT NULL, meta TEXT NOT NULL, "
                      "signature TEXT NOT NULL, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS custody (seq INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id TEXT NOT NULL, event TEXT NOT NULL, "
                      "actor TEXT NOT NULL, purpose TEXT NOT NULL, at REAL NOT NULL, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL)")
            for t in ("evidence", "custody"):
                for op in ("UPDATE", "DELETE"):
                    c.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        return c

    # ── ingest ──
    def ingest(self, env: EvidenceEnvelope) -> Receipt:
        key = self.keys.get(env.key_id)
        if key is None or key.revoked:
            raise EvidenceRejected("unknown_key", "the producer key is unknown or revoked")
        if key.producer != env.producer:
            raise EvidenceRejected("producer_mismatch", "the key is registered to a different producer")
        if env.tenant not in key.tenants:
            raise EvidenceRejected("tenant_not_allowed", "this producer key may not produce evidence for that tenant")
        want = hmac.new(key.secret, _canon(env.header()).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, env.signature or ""):
            raise EvidenceRejected("bad_signature", "the envelope was altered or not signed by this key")
        if not isinstance(env.payload, bytes) or not env.payload:
            raise EvidenceRejected("empty", "evidence has no content")
        if not (env.coverage_from <= env.coverage_to):
            raise EvidenceRejected("bad_coverage", "the coverage window is not ordered")
        now = self.clock()
        if env.coverage_to > now + 60:
            raise EvidenceRejected("future_coverage", "evidence cannot cover a period that has not happened")
        late = max(0.0, now - env.coverage_to)
        artifact_id = "ev-" + _sha(_canon(env.header()))[:24]
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            dup = c.execute("SELECT artifact_id, known_at, late_by_s FROM evidence WHERE nonce=?", (env.nonce,)).fetchone()
            if dup is not None:
                c.execute("COMMIT")
                return Receipt(dup["artifact_id"], True, dup["known_at"], dup["late_by_s"])
            prev = c.execute("SELECT row_hash FROM evidence ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["row_hash"] if prev else GENESIS
            row = {"artifact_id": artifact_id, "tenant": env.tenant, "scope": env.scope, "kind": env.kind, "source": env.source, "producer": env.producer,
                   "key_id": env.key_id, "sha256": _sha(env.payload), "size": len(env.payload), "observed_at": env.observed_at, "known_at": now,
                   "coverage_from": env.coverage_from, "coverage_to": env.coverage_to, "late_by_s": late, "classification": env.classification,
                   "nonce": env.nonce, "meta": _canon(env.meta), "signature": env.signature}
            row_hash = _sha(prev_hash + _canon(row))
            c.execute("INSERT INTO evidence (artifact_id, tenant, scope, kind, source, producer, key_id, sha256, size, content, observed_at, known_at, "
                      "coverage_from, coverage_to, late_by_s, classification, nonce, meta, signature, prev_hash, row_hash) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (artifact_id, env.tenant, env.scope, env.kind, env.source, env.producer, env.key_id, row["sha256"], row["size"], env.payload,
                       env.observed_at, now, env.coverage_from, env.coverage_to, late, env.classification, env.nonce, row["meta"], env.signature,
                       prev_hash, row_hash))
            self._custody(c, artifact_id, "ingested", env.producer, "collection", now)
            c.execute("COMMIT")
        except sqlite3.IntegrityError:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            same = self._conn().execute("SELECT artifact_id, known_at, late_by_s FROM evidence WHERE artifact_id=?", (artifact_id,)).fetchone()
            if same is not None:
                return Receipt(same["artifact_id"], True, same["known_at"], same["late_by_s"])
            raise
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()
        return Receipt(artifact_id, False, now, late)

    def _custody(self, c: sqlite3.Connection, artifact_id: str, event: str, actor: str, purpose: str, at: float) -> None:
        prev = c.execute("SELECT row_hash FROM custody ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = prev["row_hash"] if prev else GENESIS
        row = {"artifact_id": artifact_id, "event": event, "actor": actor, "purpose": purpose, "at": at}
        c.execute("INSERT INTO custody (artifact_id, event, actor, purpose, at, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?)",
                  (artifact_id, event, actor, purpose, at, prev_hash, _sha(prev_hash + _canon(row))))

    # ── read ──
    def find(self, tenant: str, *, scope: str = "", kind: str = "", since: float = 0.0, until: float = 1e18) -> list:
        """Stored metadata for evidence of one tenant whose coverage overlaps [since, until]. No payload bytes."""
        q = "SELECT * FROM evidence WHERE tenant=? AND coverage_to>=? AND coverage_from<=?"
        a: list = [tenant, since, until]
        if scope:
            q += " AND scope=?"
            a.append(scope)
        if kind:
            q += " AND kind=?"
            a.append(kind)
        with self._conn() as c:
            return [self._meta(r) for r in c.execute(q + " ORDER BY seq", a)]

    @staticmethod
    def _meta(r) -> Stored:
        return Stored(r["artifact_id"], r["tenant"], r["scope"], r["kind"], r["source"], r["producer"], r["key_id"], r["sha256"], r["size"], r["observed_at"],
                      r["known_at"], r["coverage_from"], r["coverage_to"], r["late_by_s"], r["classification"], json.loads(r["meta"]))

    def read(self, artifact_id: str, *, tenant: str, actor: str, purpose: str) -> bytes:
        """The payload, only for its own tenant, only if its digest still matches; the read is recorded."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM evidence WHERE artifact_id=?", (artifact_id,)).fetchone()
            if r is None or r["tenant"] != tenant:
                raise EvidenceUnavailable("no such evidence for this tenant")
            data = bytes(r["content"])
            if _sha(data) != r["sha256"]:
                raise EvidenceUnavailable(f"{artifact_id}: the stored bytes no longer match their digest")
            c.execute("BEGIN IMMEDIATE")
            self._custody(c, artifact_id, "read", actor, purpose, self.clock())
            c.execute("COMMIT")
            return data

    def verify(self, artifact_id: str) -> tuple:
        """(ok, reason) for one artifact: present, digest matches, signature verifies against a registered key."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM evidence WHERE artifact_id=?", (artifact_id,)).fetchone()
        if r is None:
            return False, "missing"
        if _sha(bytes(r["content"])) != r["sha256"]:
            return False, "payload does not match its digest"
        key = self.keys.get(r["key_id"])
        if key is None:
            return False, "the producer key is not registered"
        header = {"tenant": r["tenant"], "scope": r["scope"], "kind": r["kind"], "source": r["source"], "producer": r["producer"], "key_id": r["key_id"],
                  "sha256": r["sha256"], "observed_at": r["observed_at"], "coverage_from": r["coverage_from"], "coverage_to": r["coverage_to"],
                  "nonce": r["nonce"], "classification": r["classification"], "meta": json.loads(r["meta"])}
        if not hmac.compare_digest(hmac.new(key.secret, _canon(header).encode(), hashlib.sha256).hexdigest(), r["signature"]):
            return False, "the stored header no longer matches its signature"
        return True, ""

    def verify_all(self) -> dict:
        """Recompute every payload digest, signature and the whole evidence and custody hash chains."""
        problems: list = []
        with self._conn() as c:
            prev, n = GENESIS, 0
            for r in c.execute("SELECT * FROM evidence ORDER BY seq"):
                n += 1
                row = {"artifact_id": r["artifact_id"], "tenant": r["tenant"], "scope": r["scope"], "kind": r["kind"], "source": r["source"],
                       "producer": r["producer"], "key_id": r["key_id"], "sha256": r["sha256"], "size": r["size"], "observed_at": r["observed_at"],
                       "known_at": r["known_at"], "coverage_from": r["coverage_from"], "coverage_to": r["coverage_to"], "late_by_s": r["late_by_s"],
                       "classification": r["classification"], "nonce": r["nonce"], "meta": r["meta"], "signature": r["signature"]}
                if r["prev_hash"] != prev or _sha(prev + _canon(row)) != r["row_hash"]:
                    problems.append(f"evidence chain broken at seq {r['seq']} ({r['artifact_id']})")
                prev = r["row_hash"]
                ok, why = self.verify(r["artifact_id"])
                if not ok:
                    problems.append(f"{r['artifact_id']}: {why}")
            cprev = GENESIS
            for r in c.execute("SELECT * FROM custody ORDER BY seq"):
                row = {"artifact_id": r["artifact_id"], "event": r["event"], "actor": r["actor"], "purpose": r["purpose"], "at": r["at"]}
                if r["prev_hash"] != cprev or _sha(cprev + _canon(row)) != r["row_hash"]:
                    problems.append(f"custody chain broken at seq {r['seq']}")
                cprev = r["row_hash"]
            ids = {r["artifact_id"] for r in c.execute("SELECT artifact_id FROM evidence")}
            for r in c.execute("SELECT DISTINCT artifact_id FROM custody"):
                if r["artifact_id"] not in ids:
                    problems.append(f"custody records for {r['artifact_id']} but the evidence is gone")
        return {"ok": not problems, "evidence": n, "problems": problems, "head": prev}

    def head(self) -> str:
        """The hash of the newest evidence row: anchor it somewhere the vault's writer cannot reach to detect a consistent rebuild."""
        with self._conn() as c:
            r = c.execute("SELECT row_hash FROM evidence ORDER BY seq DESC LIMIT 1").fetchone()
        return r["row_hash"] if r else GENESIS

    def custody_of(self, artifact_id: str) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT event, actor, purpose, at FROM custody WHERE artifact_id=? ORDER BY seq", (artifact_id,))]
