"""Assessments: one control, one scope, one period, one result, and the evidence behind it.

Results: PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW. The rules that matter:

  * PASS needs ALL of: evidence of the right kind for the scope and period; every artifact verified (digest, signature, producer key); the period covered to the
    control's minimum; a complete population (the producer says so, signed); every tested item meeting the procedure; and no pending manual dependency.
  * No evidence is NOT_TESTED. Unverifiable or tampered evidence, a gap in coverage, an incomplete population, a mismatched scan target or an unreadable payload is
    INCONCLUSIVE. A failed item is FAIL. Missing data is never a PASS and a producer's own "compliant" claim is never read: only the data is.
  * A control with a manual dependency ends MANUAL_REVIEW unless an unexpired attestation from someone other than the control owner is in the period.
  * NOT_APPLICABLE only when an explicit applicability record says so; it is never inferred from absence.
Design effectiveness (the procedure is sound) and operating effectiveness (it held over the period) are different claims; a PASS here is a tested control for one
period, not an attestation, and there is no aggregate score.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .controls import CATALOG, ControlDef, by_id
from .vault import EvidenceUnavailable, EvidenceVault

PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW = "PASS", "FAIL", "INCONCLUSIVE", "NOT_TESTED", "NOT_APPLICABLE", "MANUAL_REVIEW"
RESULTS = (PASS, FAIL, INCONCLUSIVE, NOT_TESTED, NOT_APPLICABLE, MANUAL_REVIEW)
DAY = 86_400.0


@dataclass(frozen=True)
class Assessment:
    control_id: str
    control_version: str
    tenant: str
    scope: str
    period_from: float
    period_to: float
    result: str
    reason: str
    evidence_ids: tuple = ()
    population: int = 0
    tested: int = 0
    failures: tuple = ()
    coverage: float = 0.0
    assessor: str = ""
    at: float = 0.0
    supersedes: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["evidence_ids"], d["failures"] = list(self.evidence_ids), list(self.failures)
        return d


@dataclass
class _Outcome:
    population: int = 0
    tested: int = 0
    failures: list = field(default_factory=list)
    inconclusive: str = ""               # why the data cannot support a result
    complete: bool = False               # the producer states the population is complete


# ── procedures: each reads parsed payloads (one per verified artifact) and returns an _Outcome ──
def _population(payloads: list, key: str) -> tuple:
    items, complete = [], bool(payloads)
    for p in payloads:
        items += list(p.get(key) or [])
        complete = complete and bool(p.get("population_complete"))
    return items, complete


def mfa_coverage(payloads, ctl, period):
    items, complete = _population(payloads, "accounts")
    priv = [a for a in items if a.get("privileged")]
    o = _Outcome(len(priv), len(priv), complete=complete)
    o.failures = [f"account {a.get('id')} is privileged without MFA" for a in priv if a.get("mfa") is not True]
    return o


def access_review(payloads, ctl, period):
    items, complete = _population(payloads, "accounts")
    priv = [a for a in items if a.get("privileged") and a.get("status") != "terminated"]
    o = _Outcome(len(priv), len(priv), complete=complete)
    for a in priv:
        rb, ra = a.get("reviewed_by"), a.get("reviewed_at")
        if not rb or ra is None:
            o.failures.append(f"account {a.get('id')} has no access review in the period")
        elif not (period[0] <= float(ra) <= period[1]):
            o.failures.append(f"account {a.get('id')} was last reviewed outside the period")
        elif rb == a.get("owner") or rb == a.get("id"):
            o.failures.append(f"account {a.get('id')} was reviewed by its own owner")
    return o


def termination_revocation(payloads, ctl, period):
    items, complete = _population(payloads, "accounts")
    gone = [a for a in items if a.get("status") == "terminated" and a.get("terminated_at") is not None
            and period[0] <= float(a["terminated_at"]) <= period[1]]
    o = _Outcome(len(gone), len(gone), complete=complete)
    for a in gone:
        rv = a.get("access_revoked_at")
        if rv is None or float(rv) - float(a["terminated_at"]) > DAY:
            o.failures.append(f"account {a.get('id')} kept access more than a day after leaving")
    return o


def change_management(payloads, ctl, period):
    items, complete = _population(payloads, "releases")
    items = [r for r in items if r.get("deployed_at") is None or period[0] <= float(r["deployed_at"]) <= period[1]]
    o = _Outcome(len(items), len(items), complete=complete)
    for r in items:
        approvers = [a for a in (r.get("approvers") or []) if a != r.get("author")]
        if not approvers:
            o.failures.append(f"release {r.get('id')} had no approval independent of its author")
        missing = set(r.get("required_checks") or []) - set(r.get("passed_checks") or [])
        if missing or not r.get("required_checks"):
            o.failures.append(f"release {r.get('id')} did not pass the required checks ({sorted(missing) or 'none were recorded'})")
    return o


def monitoring_coverage(payloads, ctl, period):
    return _Outcome(1, 1, complete=True)          # decided by the coverage rule alone


def host_baseline_scan(payloads, ctl, period, scope_kind: str = "host"):
    o = _Outcome(len(payloads), len(payloads), complete=bool(payloads))
    for p in payloads:
        if p.get("target_type") != scope_kind:
            o.inconclusive = f"the scan target is a {p.get('target_type')!r}, not a {scope_kind}: it does not support this control"
        elif int(p.get("critical_open", 0) or 0) > 0:
            o.failures.append(f"{p.get('target')}: {p.get('critical_open')} critical finding(s) open")
        if not isinstance(p.get("scanned_at"), (int, float)) or not (period[0] <= float(p["scanned_at"]) <= period[1]):
            o.inconclusive = o.inconclusive or "the scan is not inside the period"
    return o


def backup_restore(payloads, ctl, period):
    backups = [b for p in payloads for b in (p.get("backups") or [])]
    restores = [r for p in payloads for r in (p.get("restore_tests") or []) if period[0] <= float(r.get("at", -1)) <= period[1]]
    o = _Outcome(len(backups), len(backups), complete=all(bool(p.get("population_complete")) for p in payloads) if payloads else False)
    o.failures = [f"backup {b.get('id')} failed" for b in backups if not b.get("ok")]
    if not restores:
        o.failures.append("no restore test in the period: a successful backup log is not evidence that a restore works")
    elif not any(r.get("ok") for r in restores):
        o.failures.append("every restore test in the period failed")
    return o


def reconciliation_open(payloads, ctl, period, threshold_s: float = 7 * DAY):
    items, complete = _population(payloads, "outcomes")
    o = _Outcome(len(items), len(items), complete=complete)
    end = period[1]
    for it in items:
        if it.get("state") == "unknown" or it.get("resolved_at") is None:
            age = end - float(it.get("opened_at", end))
            if age > threshold_s:
                o.failures.append(f"outcome {it.get('id')} has been unknown for {int(age // DAY)} days")
    return o


PROCEDURES: dict = {
    "mfa_coverage": mfa_coverage, "access_review": access_review, "termination_revocation": termination_revocation, "change_management": change_management,
    "monitoring_coverage": monitoring_coverage, "host_baseline_scan": host_baseline_scan, "backup_restore": backup_restore, "reconciliation_open": reconciliation_open,
}


def _covered(stored: list, lo: float, hi: float) -> float:
    spans = sorted((max(s.coverage_from, lo), min(s.coverage_to, hi)) for s in stored if s.coverage_to >= lo and s.coverage_from <= hi)
    covered, cur_end = 0.0, lo
    for a, b in spans:
        if b <= a:
            continue
        a = max(a, cur_end)
        if b > a:
            covered += b - a
            cur_end = b
    return covered / (hi - lo) if hi > lo else 0.0


class Assessor:
    def __init__(self, vault: EvidenceVault, *, catalog=CATALOG, clock: Callable[[], float] = time.time, procedures: Optional[dict] = None) -> None:
        self.vault, self.catalog, self.clock = vault, {c.control_id: c for c in catalog}, clock
        self.procedures = dict(PROCEDURES if procedures is None else procedures)
        self.not_applicable: dict = {}          # (tenant, scope, control_id) -> {"by": ..., "reason": ...}

    def declare_not_applicable(self, tenant: str, scope: str, control_id: str, *, by: str, reason: str) -> None:
        if not by.strip() or len(reason.strip()) < 15:
            raise ValueError("a named person and a reason are needed to declare a control not applicable")
        self.not_applicable[(tenant, scope, control_id)] = {"by": by.strip(), "reason": reason.strip()}

    def assess(self, control_id: str, tenant: str, scope: str, period_from: float, period_to: float, *, assessor: str = "edge-sentinel",
               owner: str = "", supersedes: str = "") -> Assessment:
        ctl = self.catalog.get(control_id)
        base = dict(control_id=control_id, control_version=ctl.version if ctl else "", tenant=tenant, scope=scope, period_from=period_from,
                    period_to=period_to, assessor=assessor, at=self.clock(), supersedes=supersedes)
        if ctl is None:
            return Assessment(**base, result=NOT_TESTED, reason="the control is not in the catalog")
        if (tenant, scope, control_id) in self.not_applicable:
            na = self.not_applicable[(tenant, scope, control_id)]
            return Assessment(**base, result=NOT_APPLICABLE, reason=f"declared not applicable by {na['by']}: {na['reason']}")
        stored = self.vault.find(tenant, scope=scope, since=period_from, until=period_to)
        wanted = [s for s in stored if s.kind in ctl.evidence_kinds and s.kind != "attestation"]
        attest = [s for s in stored if s.kind == "attestation"]
        proc = self.procedures.get(ctl.procedure) if ctl.procedure else None
        if proc is None:
            return self._manual(ctl, base, attest, owner)
        if not wanted:
            return Assessment(**base, result=NOT_TESTED, reason="no evidence of the required kind for this scope and period")
        # every artifact must verify, or the whole assessment cannot rest on it
        bad = [(s.artifact_id, why) for s in wanted for ok, why in [self.vault.verify(s.artifact_id)] if not ok]
        ids = tuple(s.artifact_id for s in wanted)
        if bad:
            return Assessment(**base, result=INCONCLUSIVE, evidence_ids=ids, reason=f"evidence failed verification: {bad[0][0]}: {bad[0][1]}")
        coverage = _covered(wanted, period_from, period_to)
        if coverage + 1e-9 < ctl.min_coverage:
            return Assessment(**base, result=INCONCLUSIVE, evidence_ids=ids, coverage=coverage,
                              reason=f"the evidence covers {coverage:.0%} of the period; {ctl.min_coverage:.0%} is required (a gap is not a pass)")
        payloads = []
        for s in wanted:
            try:
                payloads.append(json.loads(self.vault.read(s.artifact_id, tenant=tenant, actor=assessor, purpose=f"assess {control_id}")))
            except (ValueError, EvidenceUnavailable) as e:
                return Assessment(**base, result=INCONCLUSIVE, evidence_ids=ids, coverage=coverage, reason=f"evidence is unreadable: {type(e).__name__}")
        out = proc(payloads, ctl, (period_from, period_to))
        common = dict(evidence_ids=ids, population=out.population, tested=out.tested, failures=tuple(out.failures), coverage=coverage)
        if out.inconclusive:
            return Assessment(**base, **common, result=INCONCLUSIVE, reason=out.inconclusive)
        if out.failures:
            return Assessment(**base, **common, result=FAIL, reason=f"{len(out.failures)} item(s) did not meet the procedure")
        if not out.complete:
            return Assessment(**base, **common, result=INCONCLUSIVE, reason="the producer did not state that the population is complete")
        if out.tested < out.population:
            return Assessment(**base, **common, result=INCONCLUSIVE, reason="not every item in the population was tested")
        if out.population == 0 and not ctl.empty_ok and ctl.procedure != "monitoring_coverage":
            return Assessment(**base, **common, result=INCONCLUSIVE,
                              reason="the population is empty: a collector that found nothing is indistinguishable from one that failed (declare the control not applicable if nothing is in scope)")
        if ctl.manual_dependency:
            m = self._manual(ctl, base, attest, owner)
            return Assessment(**base, **common, result=m.result, reason=m.reason)
        return Assessment(**base, **common, result=PASS, reason=f"{out.tested} of {out.population} item(s) met the procedure over the period")

    def _manual(self, ctl: ControlDef, base: dict, attest: list, owner: str) -> Assessment:
        valid = []
        for s in attest:
            ok, _ = self.vault.verify(s.artifact_id)
            if not ok:
                continue
            try:
                p = json.loads(self.vault.read(s.artifact_id, tenant=base["tenant"], actor="edge-sentinel", purpose=f"attestation for {ctl.control_id}"))
            except (ValueError, EvidenceUnavailable):
                continue
            if (p.get("control_id") == ctl.control_id and p.get("attested_by") and p.get("attested_by") != owner and p.get("attested_by") != s.producer
                    and float(p.get("expires_at", 0)) >= base["period_to"]):
                valid.append(s.artifact_id)
        if valid:
            return Assessment(**base, result=MANUAL_REVIEW, evidence_ids=tuple(valid),
                              reason="an unexpired attestation by someone other than the owner is on file; a reviewer must still accept it")
        return Assessment(**base, result=MANUAL_REVIEW, reason=f"needs a person: {ctl.manual_dependency or 'no automated procedure exists'}")
