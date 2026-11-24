"""A quality finding as a security case, closed only on a re-test that passed.

The chain, every step on a contract the products already have:

1. **Escalate.** A QA run that ended without a pass (``FAILED``, ``NOT_COMPLETED``, timed out ...) is reported to Edge Sentinel as an
   agent event (``POST /api/v1/agent-events``, kind ``qa.finding``). Sentinel turns it into an ``INVESTIGATING`` case with the event as
   content-addressed evidence. The event names the run (``subject = qa-run:<run id>``) and carries the digest of the run's evidence
   manifest, recomputed by QA's own verifier at this moment. A finding proposes no action.
2. **Remediate.** The fix happens where the defect is. Projects does not pretend to apply it. Where the remedy is an edge block, it is
   Sentinel's own governed action (propose, decide, execute; ``approvals.py``).
3. **Re-test.** A new QA run of the SAME target and profile. It needs a person's approval like any run (``approvals.qa_approval``).
4. **Resolve.** A person closes the case. Projects reads the re-test run fresh, requires a passing outcome, the same target and profile as
   the finding run and an evidence bundle that QA's verifier accepts, and only then mints a single-use approval bound to the case and to
   that exact record (``POST /api/v1/cases/{id}/resolution``). Sentinel checks the record against the case again and keeps it as evidence.

Projects keeps no copy of any of it: the case lives in Sentinel, the runs in QA.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import time
from typing import Any, Dict, List, Mapping, Optional

from . import qa, sentinel
from .approvals import _mint, _require_ready
from .client import ProductError
from .hub import ProductHub

KIND_FINDING, KIND_RETEST = "qa.finding", "qa.retest"
CAP_RESOLVE = "sentinel.resolve_case"
SEVERITIES = ("medium", "high", "critical")
ENDED_WITHOUT_RUNNING = ("rejected", "refused", "cancelled")       # a person's or the product's refusal is not a finding about the target


def canonical(obj: Any) -> str:
    """Edge Sentinel's canonical form (``apps/edge-sentinel/evidence.py``): the digest computed here must equal the one Sentinel recomputes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def resolution_digest(case_id: str, evidence: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical({"case_id": case_id, "evidence": dict(evidence)}).encode()).hexdigest()


def _epoch(iso: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return time.time()


def _manifest(verify: Mapping[str, Any]) -> str:
    res = verify.get("result") if isinstance(verify.get("result"), Mapping) else {}
    anchor = res.get("anchor") if isinstance(res.get("anchor"), Mapping) else {}
    return str(anchor.get("recomputed_manifest_digest") or "")


def _failing(bundle: Mapping[str, Any]) -> List[str]:
    out = []
    for a in bundle.get("assertions") or []:
        if isinstance(a, Mapping) and str(a.get("native_status") or "") != "NATIVE_PASS" and a.get("assertion_id"):
            out.append(str(a["assertion_id"]))
    return out


def _read_evidence(ad: qa.QaAdapter, run_id: str, who: str) -> Dict[str, Any]:
    """(manifest digest, failing assertion ids). A run that never produced a bundle has neither; that is stated, not hidden."""
    try:
        return {"manifest": _manifest(ad.verify(run_id, actor=who)), "failing": _failing(ad.evidence(run_id, actor=who))}
    except ProductError:
        return {"manifest": "", "failing": []}


def _security_product(hub: ProductHub, wanted: str = "") -> str:
    ids = [s.id for s in hub.specs() if s.kind == "security"]
    if wanted:
        if wanted not in ids:
            raise ProductError("not_found", "unknown security product", status=404)
        return wanted
    if len(ids) != 1:
        raise ProductError("invalid", "name the security product: there is not exactly one configured", status=422)
    return ids[0]


def _quality_product(hub: ProductHub, wanted: str = "") -> str:
    ids = [s.id for s in hub.specs() if s.kind == "quality"]
    if wanted:
        if wanted not in ids:
            raise ProductError("not_found", "unknown quality product", status=404)
        return wanted
    if len(ids) != 1:
        raise ProductError("invalid", "name the quality product: there is not exactly one configured", status=422)
    return ids[0]


def escalate_finding(hub: ProductHub, qa_id: str, run_id: str, *, severity: str = "high", security_product: str = "",
                     principal: str) -> Dict[str, Any]:
    """Open (or find again: the same run yields the same case) the Sentinel case for a QA run that did not pass."""
    if severity not in SEVERITIES:
        raise ProductError("invalid", f"severity must be one of {', '.join(SEVERITIES)}", status=422)
    sid = _security_product(hub, security_product)
    _require_ready(hub, qa_id)
    _require_ready(hub, sid)
    who = f"projects:{principal}"
    ad = hub.qa(qa_id)
    r = ad.run(run_id, actor=who)                                    # fresh read, never the cache
    state, outcome = str(r.get("state") or ""), str(r.get("outcome") or "")
    if state not in qa.TERMINAL:
        raise ProductError("conflict", f"the run is {state.replace('_', ' ')}; a finding is escalated when the run has ended", status=409)
    if state in ENDED_WITHOUT_RUNNING:
        raise ProductError("conflict", f"the run was {state} and tested nothing; there is no finding to escalate", status=409)
    if state == "finished" and outcome in qa.GOOD_OUTCOMES:
        raise ProductError("conflict", f"the run ended {outcome}; there is no finding to escalate", status=409)
    ev = _read_evidence(ad, run_id, who)
    what = outcome or state.replace("_", " ")
    failing = (": " + ", ".join(ev["failing"][:4])) if ev["failing"] else ""
    proof = f"evidence manifest {ev['manifest']}" if ev["manifest"] else "no evidence bundle was produced"
    detail = f"profile {r.get('profile')} on {r.get('target_id')} ended {what}{failing}; {proof}"[:500]
    event = {"kind": KIND_FINDING, "severity": severity, "subject": f"qa-run:{run_id}", "source": "redevops-qa", "detail": detail,
             "ts": _epoch(str(r.get("finished") or r.get("created") or "")), "min_severity": "info"}
    out = hub.sentinel(sid).report_event(event, actor=who)
    case_id = str(out.get("case_id") or "")
    if not case_id:
        raise ProductError("upstream", "the security product did not open a case for the finding", status=502)
    hub.invalidate(sid)
    return {"case_id": case_id, "case_status": out.get("status"), "security_product": sid, "finding_run_id": run_id, "outcome": what,
            "failing_assertions": ev["failing"], "manifest_digest": ev["manifest"], "severity": severity}


def start_retest(hub: ProductHub, qa_id: str, run_id: str, *, principal: str) -> Dict[str, Any]:
    """A new run of the finding run's target and profile, with NO seeded defect. It waits for approval like any run that needs one."""
    _require_ready(hub, qa_id)
    who = f"projects:{principal}"
    ad = hub.qa(qa_id)
    r = ad.run(run_id, actor=who)
    if str(r.get("state") or "") not in qa.TERMINAL:
        raise ProductError("conflict", "the finding run has not ended", status=409)
    new = ad.start_run({"target_id": str(r.get("target_id") or ""), "profile": str(r.get("profile") or "")}, actor=who, source="projects")
    hub.invalidate(qa_id)
    return {"finding_run_id": run_id, "retest_run_id": str(new.get("id") or new.get("run_id") or ""), "state": new.get("state"),
            "estimate_digest": new.get("estimate_digest"), "approval": qa.approval_card(hub.spec(qa_id), new)
            if str(new.get("state")) == "awaiting_approval" else None}


def retest_record(hub: ProductHub, qa_id: str, finding_run_id: str, retest_run_id: str, *, who: str) -> Dict[str, str]:
    """The record a resolution is bound to, built from FRESH reads. Raises ``ProductError`` (409) unless the re-test is a different, ended
    run of the same target and profile whose outcome is a pass and whose evidence QA's verifier accepts."""
    if finding_run_id == retest_run_id:
        raise ProductError("conflict", "the re-test must be a new run, not the run that produced the finding", status=409)
    ad = hub.qa(qa_id)
    first, again = ad.run(finding_run_id, actor=who), ad.run(retest_run_id, actor=who)
    if str(again.get("state") or "") != "finished" or str(again.get("outcome") or "") not in qa.GOOD_OUTCOMES:
        got = str(again.get("outcome") or again.get("state") or "unknown").replace("_", " ")
        raise ProductError("conflict", f"the re-test ended {got}; a case is closed only on a re-test that passed", status=409)
    for k in ("target_id", "profile"):
        if str(first.get(k) or "") != str(again.get(k) or ""):
            raise ProductError("conflict", f"the re-test ran a different {k.replace('_id', '')} than the finding run", status=409)
    if str(again.get("defect") or "none") != "none":
        raise ProductError("conflict", "the re-test ran with a seeded defect; it does not show the target as it is", status=409)
    v = ad.verify(retest_run_id, actor=who)
    res = v.get("result") if isinstance(v.get("result"), Mapping) else {}
    manifest = _manifest(v)
    if not res.get("accepted") or not manifest:
        raise ProductError("conflict", "the re-test's evidence did not verify; the case stays open", status=409)
    return {"kind": KIND_RETEST, "run_id": retest_run_id, "outcome": str(again.get("outcome")), "manifest_digest": manifest,
            "finding_run_id": finding_run_id, "profile": str(again.get("profile") or ""), "target_id": str(again.get("target_id") or "")}


def resolve_case(hub: ProductHub, sentinel_id: str, case_id: str, *, finding_run_id: str, retest_run_id: str, quality_product: str = "",
                 principal: str) -> Dict[str, Any]:
    qid = _quality_product(hub, quality_product)
    _require_ready(hub, sentinel_id)
    _require_ready(hub, qid)
    who = f"projects:{principal}"
    record = retest_record(hub, qid, finding_run_id, retest_run_id, who=who)
    digest = resolution_digest(case_id, record)
    tok = _mint(hub, sentinel_id, operator=sentinel.OPERATOR, capability=CAP_RESOLVE, inputs={"case_id": case_id, "digest": digest},
                principal=principal, approved=True)
    out = hub.sentinel(sentinel_id).resolve_case(case_id, evidence=record, digest=digest, approval_token=tok, actor=who)
    hub.invalidate(sentinel_id)
    return {"case_id": case_id, "case_status": out.get("status"), "evidence_id": out.get("evidence_id"), "retest": record}


__all__ = ["escalate_finding", "start_retest", "retest_record", "resolve_case", "resolution_digest", "canonical", "KIND_FINDING", "KIND_RETEST",
           "CAP_RESOLVE", "SEVERITIES"]
