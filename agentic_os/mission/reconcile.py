"""Reconciliation: turn an uncertain reservation into a certain one, only when something independent can prove it.

A reservation that may already have produced an effect (DISPATCHED, or UNKNOWN after a timeout or a crash) keeps counting against
the owner's limits and can never be retried by minting another authorization. This module is how such a reservation is resolved
without a human when the evidence allows it, and handed to a human when it does not. A restart must never turn uncertainty into
permission to repeat a side effect, so nothing here treats "no answer" as "did not happen".

Rules, applied to every outstanding reservation:
* RESERVED (never dispatched). In the production profile no authorization can exist for it (they are minted only at dispatch), so one
  older than the authorization lifetime is released. Outside production a RESERVED lease may still have been used by a caller that never
  marked it, so it is released only when a probe says the effect did not happen.
* DISPATCHED / UNKNOWN. A probe (an independent lookup for the capability) answers one of three things:
    EXECUTED      -> settled, with the provider reference recorded
    NOT_EXECUTED  -> released (the provider authoritatively says it did not happen)
    UNKNOWN       -> stays counted, flagged `needs_review`, reported for a human and for Edge Sentinel
  No probe for the capability means UNKNOWN. A probe that raises is UNKNOWN. A probe that says NOT_EXECUTED for a lease that already
  has a provider reference is a contradiction and is refused (UNKNOWN), never resolved in favour of "safe to repeat".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Callable

from .authority import AuthorityGuard, Lease
from .profile import is_production

#: runtime authorizations live 120 s (execution_auth.DEFAULT_TTL_S); a margin keeps a slow dispatcher from racing the release
RELEASE_UNDISPATCHED_AFTER_S = 900.0


class ReconciliationError(RuntimeError):
    """The reconciliation request is not valid (nothing to reconcile, missing evidence, missing result)."""


class ReconciliationConflict(ReconciliationError):
    """The reservation was already resolved the other way (or a concurrent resolver won). Nothing was changed."""


#: what a person may cite. A bare assertion ("trust me") is not evidence: a kind AND a reference to something checkable.
EVIDENCE_KINDS = ("provider_record", "provider_log", "bank_statement", "mailbox_record", "ledger_entry", "screenshot", "other")


def validate_evidence(evidence) -> dict:
    """The evidence a manual reconciliation must carry, normalised: {kind, reference, provider_ref?, observed_at?, note?}."""
    if not isinstance(evidence, dict):
        raise ReconciliationError("evidence is required: say what you checked and where (kind and reference)")
    kind = str(evidence.get("kind", "")).strip().lower()
    ref = str(evidence.get("reference", "")).strip()
    if kind not in EVIDENCE_KINDS:
        raise ReconciliationError(f"evidence.kind must be one of {list(EVIDENCE_KINDS)}")
    if len(ref) < 4:
        raise ReconciliationError("evidence.reference must point at something checkable (a record id, a log line, a statement line)")
    out = {"kind": kind, "reference": ref[:500]}
    for k in ("provider_ref", "observed_at", "note"):
        if evidence.get(k):
            out[k] = str(evidence[k])[:500]
    if kind == "other" and len(out.get("note", "")) < 20:
        raise ReconciliationError("evidence of kind 'other' needs a note explaining what was checked")
    return out


class Outcome(str, Enum):
    EXECUTED = "executed"
    NOT_EXECUTED = "not_executed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    outcome: Outcome
    provider_ref: str = ""
    actual: "dict | None" = None          # actual consumption when it differs from the reservation
    reason: str = ""


Probe = Callable[[dict], Finding]


def idempotency_probe(lookup: "Callable[[str, dict], bool | None]", *, authoritative_absence: bool = False) -> Probe:
    """Build a probe from a provider lookup `lookup(idempotency_key, lease_record) -> True | False | None`.
    True means the provider has the effect; None means it could not say. False means "I find nothing", which proves the effect did
    not happen ONLY if the provider's view is authoritative (a strongly consistent lookup by the key we sent). Pass
    `authoritative_absence=True` only for such a provider; otherwise False stays UNKNOWN, because an eventually-consistent
    "not found" is exactly how a duplicate side effect is born."""
    def probe(lease: dict) -> Finding:
        key = (lease.get("meta") or {}).get("idempotency_key") or ""
        if not key:
            return Finding(Outcome.UNKNOWN, reason="the reservation recorded no idempotency key to look up")
        found = lookup(key, lease)
        if found is True:
            return Finding(Outcome.EXECUTED, reason="the provider has an effect for this idempotency key")
        if found is False and authoritative_absence:
            return Finding(Outcome.NOT_EXECUTED, reason="the provider authoritatively has no effect for this idempotency key")
        return Finding(Outcome.UNKNOWN, reason="the provider could not confirm either way")
    return probe


@dataclass
class Report:
    settled: list = field(default_factory=list)
    released: list = field(default_factory=list)
    kept: list = field(default_factory=list)            # still counted, resolved by nobody yet
    needs_review: list = field(default_factory=list)    # [{lease_id, capability, mission_id, reason}] for a human
    errors: list = field(default_factory=list)

    def summary(self) -> dict:
        return {"settled": len(self.settled), "released": len(self.released), "kept": len(self.kept),
                "needs_review": len(self.needs_review)}


class Reconciler:
    def __init__(self, guard: AuthorityGuard, probes: "dict[str, Probe] | None" = None, *,
                 release_undispatched_after_s: float = RELEASE_UNDISPATCHED_AFTER_S, clock=time.time, on_event=None) -> None:
        self.guard = guard
        self.probes: "dict[str, Probe]" = dict(probes or {})
        self.release_after = release_undispatched_after_s
        self.clock = clock
        self.on_event = on_event or (lambda ev: None)

    def register(self, capability_pattern: str, probe: Probe) -> None:
        self.probes[capability_pattern] = probe

    def _probe_for(self, capability: str) -> "Probe | None":
        for pattern, probe in self.probes.items():
            if fnmatchcase(capability, pattern):
                return probe
        return None

    def _emit(self, kind: str, lease: dict, **extra) -> None:
        meta = lease.get("meta") or {}
        try:
            self.on_event({"type": kind, "lease_id": lease["lease_id"], "capability": meta.get("capability", ""),
                           "mission_id": meta.get("mission_id", ""), "tenant": meta.get("tenant", ""), "ts": self.clock(), **extra})
        except Exception:  # noqa: BLE001 - an observer must never change a reconciliation
            pass

    def run_once(self, key: "str | None" = None) -> Report:
        rep = Report()
        for lease in self.guard.outstanding(key, detail=True):
            try:
                self._one(lease, rep)
            except Exception as e:  # noqa: BLE001 - one bad record must not stop the rest
                rep.errors.append({"lease_id": lease["lease_id"], "error": f"{type(e).__name__}: {e}"})
        return rep

    def _one(self, lease: dict, rep: Report) -> None:
        lid, state = lease["lease_id"], lease["state"]
        meta = lease.get("meta") or {}
        cap = meta.get("capability", "")
        probe = self._probe_for(cap)
        if state == Lease.RESERVED.value:
            age = self.clock() - float(lease.get("updated") or lease.get("created") or 0)
            if is_production() and age >= self.release_after:
                if self.guard.release(lid):
                    self.guard.annotate(lid, reconcile_status="released_never_dispatched")
                    rep.released.append(lid)
                    self._emit("LEASE_RELEASED", lease, reason="never dispatched; no authorization can exist for it")
                return
            if probe is not None:
                f = self._ask(probe, lease)
                if f.outcome is Outcome.NOT_EXECUTED and not meta.get("provider_ref") and self.guard.release(lid):
                    self.guard.annotate(lid, reconcile_status="released_by_probe")
                    rep.released.append(lid)
                    self._emit("LEASE_RELEASED", lease, reason=f.reason)
                    return
            self.guard.annotate(lid, reconcile_status="awaiting_verification")
            rep.kept.append(lid)
            return
        # DISPATCHED / UNKNOWN: the effect may exist
        if probe is None:
            return self._keep(lease, rep, f"no independent lookup is registered for {cap or 'this capability'}")
        f = self._ask(probe, lease)
        attempts = int(meta.get("reconcile_attempts", 0)) + 1
        self.guard.annotate(lid, reconcile_attempts=attempts, last_reconcile_at=self.clock())
        if f.outcome is Outcome.EXECUTED:
            if self.guard.reconcile(lid, executed=True, actual=f.actual):
                self.guard.annotate(lid, provider_ref=f.provider_ref or None, reconcile_status="settled_by_probe")
                rep.settled.append(lid)
                self._emit("LEASE_SETTLED", lease, reason=f.reason, provider_ref=f.provider_ref)
            return
        if f.outcome is Outcome.NOT_EXECUTED:
            if meta.get("provider_ref"):
                return self._keep(lease, rep, "a probe says it did not happen but a provider reference was recorded: "
                                              "contradictory evidence, not resolved")
            if self.guard.reconcile(lid, executed=False):
                self.guard.annotate(lid, reconcile_status="released_by_probe")
                rep.released.append(lid)
                self._emit("LEASE_RELEASED", lease, reason=f.reason)
            return
        self._keep(lease, rep, f.reason or "the independent check could not tell")

    def _ask(self, probe: Probe, lease: dict) -> Finding:
        try:
            f = probe(lease)
            return f if isinstance(f, Finding) else Finding(Outcome.UNKNOWN, reason="the probe returned no finding")
        except Exception as e:  # noqa: BLE001 - "I could not check" is UNKNOWN, never "it did not happen"
            return Finding(Outcome.UNKNOWN, reason=f"the probe failed: {type(e).__name__}: {e}")

    def _keep(self, lease: dict, rep: Report, reason: str) -> None:
        lid = lease["lease_id"]
        meta = lease.get("meta") or {}
        self.guard.annotate(lid, reconcile_status="needs_review", reconcile_reason=reason)
        rep.kept.append(lid)
        rep.needs_review.append({"lease_id": lid, "capability": meta.get("capability", ""),
                                 "mission_id": meta.get("mission_id", ""), "tenant": meta.get("tenant", ""), "reason": reason})
        self._emit("UNKNOWN_OUTCOME", lease, reason=reason)


def resolve_manually(guard: AuthorityGuard, lease_id: str, *, executed: bool, by: str, reason: str, provider_ref: str = "") -> bool:
    """A named person states what happened (after checking the provider themselves). It settles or releases the reservation and
    records who said so and why on the durable lease, so the resolution is auditable. Refused without a name and a reason, and
    refused for a reservation that was never dispatched (that one is released, not 'resolved')."""
    if not by.strip() or len(reason.strip()) < 8:
        raise ValueError("a manual resolution needs the resolver's name and a reason of at least a few words")
    rec = guard.lease(lease_id)
    if rec is None or rec["state"] not in (Lease.DISPATCHED.value, Lease.UNKNOWN.value):
        return False
    ok = guard.reconcile(lease_id, executed=executed)
    if ok:
        guard.annotate(lease_id, reconcile_status="resolved_manually", resolved_by=by.strip(), resolution_reason=reason.strip(),
                       resolved_at=time.time(), provider_ref=provider_ref or None, manual_outcome="executed" if executed else "not_executed")
    return ok


def main(argv=None) -> int:
    """`python -m agentic_os.mission.reconcile list|run|resolve --db <AGENTIC_OS_AUTHORITY_DB>`: see what is outstanding, run the
    reconciler with the probes you registered in your own wiring (none by default), or record a manual resolution."""
    import argparse
    import json
    import os
    from .authority import SqliteEnvelopeStore, SqliteLedger
    ap = argparse.ArgumentParser(prog="agentic_os.mission.reconcile")
    ap.add_argument("--db", default=os.environ.get("AGENTIC_OS_AUTHORITY_DB", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("run")
    r = sub.add_parser("resolve")
    r.add_argument("lease_id")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--executed", action="store_true")
    g.add_argument("--not-executed", action="store_true")
    r.add_argument("--by", required=True)
    r.add_argument("--reason", required=True)
    r.add_argument("--provider-ref", default="")
    a = ap.parse_args(argv)
    if not a.db:
        print("--db (or AGENTIC_OS_AUTHORITY_DB) is required")
        return 2
    guard = AuthorityGuard(SqliteEnvelopeStore(a.db), SqliteLedger(a.db + ".usage"))
    if a.cmd == "list":
        print(json.dumps(guard.outstanding(detail=True), indent=2, default=str))
        return 0
    if a.cmd == "run":
        rep = Reconciler(guard).run_once()
        print(json.dumps({"summary": rep.summary(), "needs_review": rep.needs_review, "errors": rep.errors}, indent=2))
        return 1 if rep.needs_review or rep.errors else 0
    try:
        ok = resolve_manually(guard, a.lease_id, executed=a.executed, by=a.by, reason=a.reason, provider_ref=a.provider_ref)
    except ValueError as e:
        print(str(e))
        return 2
    print("resolved" if ok else "not resolved: no such dispatched/unknown reservation")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
