"""The reconciliation supervisor: durable, bounded, read-only probing of unknown outcomes.

Policy (the invariant that governs all of it): ONLY positive execution evidence or an authoritative negative resolves an UNKNOWN outcome.
Exhausting the probe retry budget never turns uncertainty into permission to resend. The supervisor repeats the READ-ONLY probe; it
never repeats the business action while the outcome is unknown.

  probe result (after the registry enforces the evidence contract)      action                                      mission
  CONFIRMED_EXECUTED with evidence                                      settle, restore the result, resume          running
  CONFIRMED_NOT_EXECUTED, authoritative and past the visibility window  release, re-gate, retry the action (once)   running
  INCONCLUSIVE (incl. a non-authoritative "not found")                  retry the probe with backoff                waiting
  TRANSIENT_ERROR (timeout, 429, 5xx)                                   retry the probe with backoff / Retry-After  waiting
  UNSUPPORTED (no reliable lookup for this provider)                    escalate immediately                        waiting
  retry budget exhausted                                                escalate to a human                         waiting
  conflicting evidence                                                  escalate immediately                        waiting

Everything that matters is durable (SQLite): probe attempts, the next eligible time, the last result, the escalation and whether its
notification went out. A restart does not reset a budget or re-notify; replicas claim a job with a compare-and-swap, and a claim that
is not renewed expires so a crashed worker's job is picked up again.
"""
from __future__ import annotations

import json
import os
import random
import socket
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .probes import ProbeContext, ProbeOutcome, ProbeRegistry, ProbeResult, Verdict
from .reconcile import ReconciliationConflict, ReconciliationError


@dataclass(frozen=True)
class RetryPolicy:
    """Starting values, not provider guarantees. Delay before probe n (n = 1..max_probes): initial * factor^(n-1), capped, with jitter."""
    max_probes: int = 5
    initial_s: float = 30.0
    max_s: float = 300.0
    factor: float = 2.0
    jitter: float = 0.2                  # +/- fraction of the delay
    max_retry_after_s: float = 3600.0    # a provider's Retry-After is honoured, but bounded

    def delay_before(self, n: int, retry_after: Optional[float] = None, rng: Optional[random.Random] = None) -> float:
        base = min(self.max_s, self.initial_s * (self.factor ** max(0, n - 1)))
        r = (rng or random).random()
        d = max(0.0, base * (1.0 + self.jitter * (2 * r - 1)))
        if retry_after:
            d = max(d, min(float(retry_after), self.max_retry_after_s))
        return d


PRESETS = {                                 # the three starting points from the review
    "fast": RetryPolicy(max_probes=5, initial_s=10, max_s=120),
    "default": RetryPolicy(max_probes=5, initial_s=30, max_s=300),
    "slow": RetryPolicy(max_probes=5, initial_s=60, max_s=900),
}


class ReconcileJobs:
    """Durable job table. Each mutation is one BEGIN IMMEDIATE transaction; ownership of a job is a claim token."""

    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS reconcile_jobs (
                job_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, node_id TEXT NOT NULL, capability TEXT NOT NULL, tenant TEXT,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                next_at REAL NOT NULL, claimed_by TEXT, claim_until REAL, claim_token INTEGER NOT NULL DEFAULT 0,
                last_outcome TEXT, last_detail TEXT, last_at REAL, dispatched_at REAL,
                escalation_reason TEXT, escalated_at REAL,
                notify_state TEXT, notify_claim_until REAL, notify_attempts INTEGER NOT NULL DEFAULT 0, notified_at REAL,
                resolution TEXT, resolved_at REAL, created REAL NOT NULL, updated REAL NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS rj_due ON reconcile_jobs (state, next_at)")
            c.execute("CREATE TABLE IF NOT EXISTS reconcile_history (job_id TEXT, ts REAL, kind TEXT, detail TEXT)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        return c

    def _tx(self, fn):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            out = fn(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    @staticmethod
    def _log(c, job_id, ts, kind, **detail):
        c.execute("INSERT INTO reconcile_history (job_id, ts, kind, detail) VALUES (?,?,?,?)",
                  (job_id, ts, kind, json.dumps(detail, sort_keys=True, default=str)))

    def get(self, job_id: str) -> "dict | None":
        with self._conn() as c:
            r = c.execute("SELECT * FROM reconcile_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def list(self, state: "str | None" = None) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM reconcile_jobs" + (" WHERE state=?" if state else "") + " ORDER BY created",
                             (state,) if state else ()).fetchall()
        return [dict(r) for r in rows]

    def history(self, job_id: str) -> list:
        with self._conn() as c:
            return [{"ts": r["ts"], "kind": r["kind"], **json.loads(r["detail"])} for r in
                    c.execute("SELECT * FROM reconcile_history WHERE job_id=? ORDER BY rowid", (job_id,))]

    def enqueue(self, job_id: str, *, mission_id: str, node_id: str, capability: str, tenant: str = "", dispatched_at: float = 0.0,
                first_at: float, max_attempts: int, now: float) -> bool:
        """Idempotent: an existing job (and so its attempts and escalation) is never reset by a restart or a rescan."""
        def go(c):
            if c.execute("SELECT 1 FROM reconcile_jobs WHERE job_id=?", (job_id,)).fetchone():
                return False
            c.execute("INSERT INTO reconcile_jobs (job_id,mission_id,node_id,capability,tenant,state,max_attempts,next_at,dispatched_at,"
                      "created,updated) VALUES (?,?,?,?,?,'pending',?,?,?,?,?)",
                      (job_id, mission_id, node_id, capability, tenant, max_attempts, first_at, dispatched_at, now, now))
            self._log(c, job_id, now, "enqueued", first_at=first_at)
            return True
        return self._tx(go)

    def claim_due(self, owner: str, now: float, *, claim_s: float = 120.0, limit: int = 10) -> list:
        def go(c):
            rows = c.execute("SELECT job_id FROM reconcile_jobs WHERE state='pending' AND next_at<=? AND (claim_until IS NULL OR claim_until<=?) "
                             "ORDER BY next_at LIMIT ?", (now, now, limit)).fetchall()
            out = []
            for r in rows:
                c.execute("UPDATE reconcile_jobs SET claimed_by=?, claim_until=?, claim_token=claim_token+1, updated=? WHERE job_id=?",
                          (owner, now + claim_s, now, r["job_id"]))
                out.append(dict(c.execute("SELECT * FROM reconcile_jobs WHERE job_id=?", (r["job_id"],)).fetchone()))
            return out
        return self._tx(go)

    def release_claim(self, job_id: str, owner: str, token: int, now: float, *, retry_at: float) -> bool:
        """Give a job back WITHOUT counting an attempt (e.g. another replica owns the mission right now)."""
        def go(c):
            cur = c.execute("UPDATE reconcile_jobs SET claimed_by=NULL, claim_until=NULL, next_at=?, updated=? "
                            "WHERE job_id=? AND claimed_by=? AND claim_token=? AND state='pending'", (retry_at, now, job_id, owner, token))
            return cur.rowcount == 1
        return self._tx(go)

    def record(self, job_id: str, owner: str, token: int, *, outcome: str, detail: str, now: float, next_at: "float | None" = None,
               escalate: "str | None" = None) -> dict:
        """Count one probe attempt and either schedule the next one or escalate. Only the current claimant can do this; the
        escalation transition (and so its notification) happens exactly once."""
        def go(c):
            j = c.execute("SELECT * FROM reconcile_jobs WHERE job_id=?", (job_id,)).fetchone()
            if j is None or j["state"] != "pending" or j["claimed_by"] != owner or j["claim_token"] != token:
                return {"ok": False, "escalated": False}
            attempts = j["attempts"] + 1
            if escalate:
                c.execute("UPDATE reconcile_jobs SET state='escalated', attempts=?, last_outcome=?, last_detail=?, last_at=?, claimed_by=NULL, "
                          "claim_until=NULL, escalation_reason=?, escalated_at=?, notify_state='pending', updated=? WHERE job_id=?",
                          (attempts, outcome, detail[:500], now, escalate[:500], now, now, job_id))
                self._log(c, job_id, now, "escalated", reason=escalate, outcome=outcome, attempts=attempts)
                return {"ok": True, "escalated": True, "attempts": attempts}
            c.execute("UPDATE reconcile_jobs SET attempts=?, last_outcome=?, last_detail=?, last_at=?, claimed_by=NULL, claim_until=NULL, "
                      "next_at=?, updated=? WHERE job_id=?", (attempts, outcome, detail[:500], now, next_at, now, job_id))
            self._log(c, job_id, now, "probed", outcome=outcome, detail=detail[:200], attempts=attempts, next_at=next_at)
            return {"ok": True, "escalated": False, "attempts": attempts}
        return self._tx(go)

    def resolve(self, job_id: str, owner: str, token: int, *, resolution: str, now: float) -> bool:
        def go(c):
            cur = c.execute("UPDATE reconcile_jobs SET state='resolved', resolution=?, resolved_at=?, claimed_by=NULL, claim_until=NULL, "
                            "updated=? WHERE job_id=? AND claimed_by=? AND claim_token=? AND state='pending'",
                            (resolution, now, now, job_id, owner, token))
            if cur.rowcount == 1:
                self._log(c, job_id, now, "resolved", resolution=resolution)
            return cur.rowcount == 1
        return self._tx(go)

    def mark_resolved(self, job_id: str, *, resolution: str, now: float) -> bool:
        """Resolved outside the supervisor (a person used the API). Unconditional for a job that is still open."""
        def go(c):
            cur = c.execute("UPDATE reconcile_jobs SET state='resolved', resolution=?, resolved_at=?, claimed_by=NULL, claim_until=NULL, "
                            "updated=? WHERE job_id=? AND state IN ('pending','escalated')", (resolution, now, now, job_id))
            if cur.rowcount == 1:
                self._log(c, job_id, now, "resolved", resolution=resolution)
            return cur.rowcount == 1
        return self._tx(go)

    def requeue(self, job_id: str, *, now: float, first_at: float, max_attempts: "int | None" = None) -> bool:
        """A person re-opens an escalated job with a fresh retry budget."""
        def go(c):
            cur = c.execute("UPDATE reconcile_jobs SET state='pending', attempts=0, next_at=?, notify_state=NULL, escalation_reason=NULL, "
                            "escalated_at=NULL, notified_at=NULL, notify_attempts=0, max_attempts=COALESCE(?, max_attempts), updated=? "
                            "WHERE job_id=? AND state='escalated'", (first_at, max_attempts, now, job_id))
            if cur.rowcount == 1:
                self._log(c, job_id, now, "requeued")
            return cur.rowcount == 1
        return self._tx(go)

    def claim_notifications(self, now: float, *, claim_s: float = 60.0, max_attempts: int = 5, limit: int = 10) -> list:
        def go(c):
            rows = c.execute("SELECT job_id FROM reconcile_jobs WHERE state='escalated' AND notify_state IN ('pending','claimed') "
                             "AND (notify_claim_until IS NULL OR notify_claim_until<=?) AND notify_attempts<? LIMIT ?",
                             (now, max_attempts, limit)).fetchall()
            out = []
            for r in rows:
                c.execute("UPDATE reconcile_jobs SET notify_state='claimed', notify_claim_until=?, notify_attempts=notify_attempts+1 WHERE job_id=?",
                          (now + claim_s, r["job_id"]))
                out.append(dict(c.execute("SELECT * FROM reconcile_jobs WHERE job_id=?", (r["job_id"],)).fetchone()))
            return out
        return self._tx(go)

    def mark_notified(self, job_id: str, now: float) -> None:
        def go(c):
            c.execute("UPDATE reconcile_jobs SET notify_state='sent', notified_at=? WHERE job_id=? AND notify_state='claimed'", (now, job_id))
            self._log(c, job_id, now, "notified")
        self._tx(go)

    def notify_failed(self, job_id: str, error: str, now: float, *, max_attempts: int = 5) -> None:
        def go(c):
            c.execute("UPDATE reconcile_jobs SET notify_state=CASE WHEN notify_attempts>=? THEN 'failed' ELSE 'pending' END, "
                      "notify_claim_until=? WHERE job_id=? AND notify_state='claimed'", (max_attempts, now + 30.0, job_id))
            self._log(c, job_id, now, "notify_failed", error=error[:200])
        self._tx(go)


@dataclass
class TickReport:
    enqueued: int = 0
    probed: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    escalated: list = field(default_factory=list)
    retried: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    notified: list = field(default_factory=list)


Notifier = Callable[[dict, str], None]       # (job, idempotency_key)


class ReconciliationSupervisor:
    def __init__(self, runtime, jobs: ReconcileJobs, registry: ProbeRegistry, *, policy: RetryPolicy = RetryPolicy(),
                 owner: "str | None" = None, clock=time.time, rng: "random.Random | None" = None, notifier: "Notifier | None" = None,
                 on_event: "Callable[[dict], None] | None" = None, mission_leases=None, claim_s: float = 120.0) -> None:
        self.rt, self.jobs, self.registry, self.policy = runtime, jobs, registry, policy
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.clock, self.rng = clock, rng or random.Random()
        self.notifier, self.on_event, self.leases, self.claim_s = notifier, on_event or (lambda ev: None), mission_leases, claim_s

    def _emit(self, kind: str, job: dict, **extra) -> None:
        try:
            self.on_event({"type": kind, "job_id": job["job_id"], "mission_id": job["mission_id"], "capability": job["capability"],
                           "tenant": job.get("tenant", ""), "ts": self.clock(), **extra})
        except Exception:  # noqa: BLE001 - an observer must never change a reconciliation
            pass

    # ── find what needs reconciling ───────────────────────────────────────────────────
    def scan(self) -> int:
        """Enqueue a job for every parked node (idempotent) and close jobs a person already resolved."""
        now, n = self.clock(), 0
        open_ids = set()
        for mid in list(self.rt.store.mission_ids()):
            if mid in getattr(self.rt, "quarantined", {}):
                continue
            for rec in self.rt.repo.pending_reconciliation(mid):
                lid = rec.get("lease_id") or ""
                if not lid:
                    continue
                open_ids.add(lid)
                lease = self.rt.authority.lease(lid) if self.rt.authority is not None else None
                dispatched = float(((lease or {}).get("meta") or {}).get("dispatched_at") or rec.get("at") or now)
                cap = rec.get("capability", "")
                first = max(now + self.policy.delay_before(1, rng=self.rng), self.registry.horizon_floor(cap, dispatched))
                if self.jobs.enqueue(lid, mission_id=mid, node_id=rec["node_id"], capability=cap,
                                     tenant=(self.rt._missions[mid].tenant if mid in self.rt._missions else ""),
                                     dispatched_at=dispatched, first_at=first, max_attempts=self.policy.max_probes, now=now):
                    n += 1
        for j in self.jobs.list():
            if j["state"] in ("pending", "escalated") and j["job_id"] not in open_ids:
                self.jobs.mark_resolved(j["job_id"], resolution="resolved_by_a_person", now=now)
        return n

    # ── one pass ─────────────────────────────────────────────────────────────────────────
    def tick(self) -> TickReport:
        rep = TickReport()
        now = self.clock()
        rep.enqueued = self.scan()
        for job in self.jobs.claim_due(self.owner, now, claim_s=self.claim_s):
            self._process(job, rep)
        self._notify(rep)
        return rep

    def _process(self, job: dict, rep: TickReport) -> None:
        now = self.clock()
        jid, token = job["job_id"], job["claim_token"]
        mid = job["mission_id"]
        pend = {p["node_id"]: p for p in self.rt.repo.pending_reconciliation(mid)}
        if job["node_id"] not in pend:                       # a person got there first
            self.jobs.resolve(jid, self.owner, token, resolution="resolved_by_a_person", now=now)
            rep.resolved.append(jid)
            return
        lease = self.rt.authority.lease(jid) if self.rt.authority is not None else None
        meta = (lease or {}).get("meta") or {}
        dispatched = float(job.get("dispatched_at") or meta.get("dispatched_at") or now)
        adapters = [(q, p) for q, p in self.registry.adapters_for(job["capability"]) if q.supports_lookup]
        attempt = int(job["attempts"]) + 1
        if not adapters:
            return self._escalate(job, token, "unsupported", "no reliable lookup is registered for this provider: it cannot be reconciled "
                                  "automatically", rep)
        verdicts: "list[Verdict]" = []
        retry_after = None
        for q, probe in adapters:
            try:
                res = probe(ProbeContext(lease=lease or {}, attempt=attempt, now=now, dispatched_at=dispatched))
            except Exception as e:  # noqa: BLE001 - "I could not look" is a transient failure, never "it did not happen"
                res = ProbeResult(ProbeOutcome.TRANSIENT_ERROR, detail=f"the probe raised {type(e).__name__}: {str(e)[:150]}")
            v = self.registry.judge(q, res, dispatched_at=dispatched, now=now)
            verdicts.append(v)
            if v.result is not None and v.result.retry_after_s:
                retry_after = max(retry_after or 0.0, v.result.retry_after_s)
        rep.probed.append(jid)
        outs = {v.outcome for v in verdicts}
        detail = "; ".join(f"{v.adapter}: {v.outcome.value}" + (f" ({'downgraded: ' if v.downgraded else ''}{v.reason})" if v.reason else "")
                           for v in verdicts)
        self._emit("RECONCILE_PROBED", job, attempt=attempt, outcomes=sorted(o.value for o in outs))
        # 1. conflicting evidence is never resolved either way
        contradiction = (ProbeOutcome.CONFIRMED_NOT_EXECUTED in outs and meta.get("provider_ref"))
        if (ProbeOutcome.CONFIRMED_EXECUTED in outs and ProbeOutcome.CONFIRMED_NOT_EXECUTED in outs) or contradiction:
            why = ("two lookups disagree: one says it executed, one says it did not" if len(outs) > 1
                   else "the lookup says it did not execute but a provider reference was recorded")
            return self._escalate(job, token, "conflict", f"conflicting evidence: {why}", rep)
        # 2. positive evidence
        executed = next((v for v in verdicts if v.outcome is ProbeOutcome.CONFIRMED_EXECUTED), None)
        if executed is not None:
            return self._apply(job, token, executed, True, rep)
        # 3. authoritative negative (already vetted by the registry: qualified, marked, evidenced, past the visibility window)
        absent = next((v for v in verdicts if v.outcome is ProbeOutcome.CONFIRMED_NOT_EXECUTED), None)
        if absent is not None:
            return self._apply(job, token, absent, False, rep)
        # 4. no adapter can support reconciliation
        if outs == {ProbeOutcome.UNSUPPORTED}:
            return self._escalate(job, token, "unsupported", verdicts[0].reason, rep)
        # 5. inconclusive or transient: repeat the READ-ONLY probe with backoff; never the business action
        if attempt >= int(job["max_attempts"]):
            return self._escalate(job, token, "exhausted", f"the probe retry budget ({job['max_attempts']}) is exhausted and the outcome is "
                                  f"still unknown ({detail}); a person must decide", rep)
        nxt = now + self.policy.delay_before(attempt + 1, retry_after, self.rng)
        nxt = max(nxt, self.registry.horizon_floor(job["capability"], dispatched))
        ok = self.jobs.record(jid, self.owner, token, outcome=sorted(o.value for o in outs)[0], detail=detail, now=now, next_at=nxt)
        if ok["ok"]:
            rep.retried.append(jid)
            self._emit("RECONCILE_RETRY_SCHEDULED", job, attempt=attempt, next_at=nxt)

    def _apply(self, job: dict, token: int, v: Verdict, executed: bool, rep: TickReport) -> None:
        jid, mid = job["job_id"], job["mission_id"]
        r = v.result
        ev = dict(r.evidence or {})
        if r.provider_ref:
            ev.setdefault("provider_ref", r.provider_ref)
        ev["note"] = (ev.get("note", "") + f" automated by {self.owner} from adapter {v.adapter}").strip()
        lease_ctx = self._mission_lease(mid)
        if lease_ctx is False:                                # another replica owns this mission right now: try again later, no attempt used
            self.jobs.release_claim(jid, self.owner, job["claim_token"], self.clock(), retry_at=self.clock() + self.policy.initial_s)
            rep.skipped.append(jid)
            return
        try:
            with (lease_ctx or _Null()):
                self.rt.reconcile_node(mid, job["node_id"], executed=executed, evidence=ev, actor=f"system:reconciliation:{v.adapter}",
                                       reason=("the provider lookup confirmed the effect" if executed else
                                               "the provider authoritatively confirmed the effect did not happen"),
                                       result=r.result if executed else None, resume=True)
        except ReconciliationConflict as e:
            return self._escalate(job, token, "conflict", f"the reservation was resolved the other way meanwhile: {e}", rep)
        except ReconciliationError as e:
            return self._escalate(job, token, "needs_a_person", str(e), rep)
        if self.jobs.resolve(jid, self.owner, token, resolution="executed" if executed else "not_executed", now=self.clock()):
            rep.resolved.append(jid)
            self._emit("RECONCILE_RESOLVED", job, executed=executed, adapter=v.adapter)

    def _mission_lease(self, mission_id: str):
        """A held, fenced mission lease for the duration of the resume, None if leases are not in use, False if another replica owns it."""
        if self.leases is None:
            return None
        lease = self.leases.acquire(mission_id, self.owner)
        return lease if lease is not None else False

    def _escalate(self, job: dict, token: int, outcome: str, reason: str, rep: TickReport) -> None:
        r = self.jobs.record(job["job_id"], self.owner, token, outcome=outcome, detail=reason, now=self.clock(), escalate=reason)
        if r["ok"] and r["escalated"]:
            rep.escalated.append(job["job_id"])
            self._emit("UNKNOWN_OUTCOME", job, reason=reason, escalation=outcome)    # the Edge Sentinel event

    def _notify(self, rep: TickReport) -> None:
        if self.notifier is None:
            return
        now = self.clock()
        for job in self.jobs.claim_notifications(now):
            key = f"escalation:{job['job_id']}:{int(job['escalated_at'] or 0)}"     # a stable idempotency key for the receiver
            try:
                self.notifier(job, key)
            except Exception as e:  # noqa: BLE001
                self.jobs.notify_failed(job["job_id"], f"{type(e).__name__}: {e}", now)
                continue
            self.jobs.mark_notified(job["job_id"], now)
            rep.notified.append(job["job_id"])


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
