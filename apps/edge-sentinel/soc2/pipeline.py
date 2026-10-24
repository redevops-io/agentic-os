"""The evidence pipeline: scheduled collection from real systems into the vault.

A `Collector` reads one source for one window and returns a payload. The pipeline owns everything around it: which windows are due, signing with the producer's own key, ingesting,
and recording what failed. The properties that matter for an assessment:

  * A FAILED collection produces NO evidence. A collector that could not read its source, was refused (a 403, a 404 that may mean "no access"), hit a page cap or timed out leaves a gap,
    and the assessor reads a gap as INCONCLUSIVE or NOT_TESTED. It never writes an empty or partial payload that says "complete" and it never turns a read error into a finding.
  * Only COMPLETED windows are collected (the current, unfinished slice is skipped), so coverage is never claimed for time that has not happened.
  * A window already covered by evidence from the same producer is skipped, so a re-run, a retry after a crash or an overlapping schedule cannot count the same thing twice.
  * The payload is the collector's data and `population_complete`; there is no field for the collector's opinion of the result, and no collector computes one.
  * Secrets (producer signing keys, source tokens) come from callables the deployment provides; they are never stored in a job, a report or an event, and error text is redacted before it is kept.

What this does not do: schedule itself (call `run` from a job runner), decide what a source's data means, or prove the collector found everything: completeness is the collector's signed
statement about the pages it read, not an inventory."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .collectors import build_envelope
from .vault import EvidenceRejected, EvidenceVault

DAY = 86_400.0


class CollectionFailed(Exception):
    """The source could not be read to the standard the evidence needs. The window is left as a gap."""


@dataclass
class Collected:
    payload: dict                 # includes `population_complete` for the kinds whose procedures need it
    meta: dict = field(default_factory=dict)


class Collector(Protocol):
    name: str
    kind: str
    source: str

    def collect(self, window_from: float, window_to: float) -> Collected: ...


@dataclass(frozen=True)
class Job:
    collector: Collector
    tenant: str
    scope: str
    producer: str
    key_id: str
    window_s: float = DAY


@dataclass(frozen=True)
class RunRecord:
    job: str
    kind: str
    tenant: str
    scope: str
    window: tuple
    status: str                   # collected | duplicate | skipped_covered | failed | rejected
    detail: str = ""
    artifact_id: str = ""


@dataclass
class Report:
    records: list = field(default_factory=list)

    def by_status(self, status: str) -> list:
        return [r for r in self.records if r.status == status]

    def gaps(self) -> list:
        """The windows that have no evidence after this run because collection failed or was refused."""
        return [r for r in self.records if r.status in ("failed", "rejected")]


def _redact(text: str, secrets: tuple) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return re.sub(r"(?i)(bearer|token|authorization)[\"'=: ]+[A-Za-z0-9_\-\.]{12,}", r"\1 [redacted]", text)[:300]


class Pipeline:
    def __init__(self, vault: EvidenceVault, secret_for: Callable[[str], bytes], *, clock: Callable[[], float] = time.time,
                 on_event: Optional[Callable[[dict], None]] = None, redact: tuple = ()) -> None:
        """`secret_for(key_id)` returns the producer's signing secret. `redact` lists source tokens whose text must never appear in a record or an event."""
        self.vault, self.secret_for, self.clock = vault, secret_for, clock
        self.on_event = on_event or (lambda ev: None)
        self.redact = tuple(redact)

    def windows(self, job: Job, since: float, until: float) -> list:
        """The completed windows of `job.window_s` between `since` and `until`, aligned to the window size. The current, unfinished window is never included."""
        w = job.window_s
        end = min(until, self.clock())
        start = (int(since // w)) * w
        out = []
        while start + w <= end + 1e-9:
            if start + w > since:
                out.append((start, start + w))
            start += w
        return out

    def _covered(self, job: Job, w0: float, w1: float) -> bool:
        for s in self.vault.find(job.tenant, scope=job.scope, kind=job.collector.kind, since=w0, until=w1):
            if s.producer == job.producer and s.coverage_from <= w0 + 1e-9 and s.coverage_to >= w1 - 1e-9:
                return True
        return False

    def run(self, jobs: list, since: float, until: float) -> Report:
        rep = Report()
        for job in jobs:
            for (w0, w1) in self.windows(job, since, until):
                rep.records.append(self._one(job, w0, w1))
        return rep

    def _one(self, job: Job, w0: float, w1: float) -> RunRecord:
        base = dict(job=job.collector.name, kind=job.collector.kind, tenant=job.tenant, scope=job.scope, window=(w0, w1))
        if self._covered(job, w0, w1):
            return RunRecord(**base, status="skipped_covered")
        try:
            got = job.collector.collect(w0, w1)
        except CollectionFailed as e:
            return self._fail(base, "failed", str(e))
        except Exception as e:  # noqa: BLE001 - whatever went wrong reading the source is a gap, not a finding
            return self._fail(base, "failed", f"{type(e).__name__}: {e}")
        if not isinstance(got, Collected) or not isinstance(got.payload, dict):
            return self._fail(base, "failed", "the collector returned no payload")
        try:
            secret = self.secret_for(job.key_id)
        except Exception as e:  # noqa: BLE001
            return self._fail(base, "failed", f"no signing key for {job.key_id}: {type(e).__name__}")
        digest = hashlib.sha256(json.dumps(got.payload, sort_keys=True, default=str).encode()).hexdigest()
        nonce = hashlib.sha256(f"{job.tenant}|{job.scope}|{job.producer}|{job.collector.kind}|{w0}|{w1}|{digest}".encode()).hexdigest()[:24]
        env = build_envelope(job.collector.kind, got.payload, tenant=job.tenant, scope=job.scope, source=job.collector.source, producer=job.producer, key_id=job.key_id,
                             secret=secret, observed_at=w1, coverage_from=w0, coverage_to=w1, nonce=nonce, meta={"collector": job.collector.name, **got.meta})
        try:
            receipt = self.vault.ingest(env)
        except EvidenceRejected as e:
            return self._fail(base, "rejected", f"{e.code}")
        return RunRecord(**base, status="duplicate" if receipt.duplicate else "collected", artifact_id=receipt.artifact_id)

    def _fail(self, base: dict, status: str, detail: str) -> RunRecord:
        rec = RunRecord(**base, status=status, detail=_redact(detail, self.redact))
        try:
            self.on_event({"type": "EVIDENCE_COLLECTION_GAP", "collector": rec.job, "kind": rec.kind, "tenant": rec.tenant, "scope": rec.scope, "window": list(rec.window),
                           "status": status, "detail": rec.detail})
        except Exception:  # noqa: BLE001 - an observer must never change a collection
            pass
        return rec
