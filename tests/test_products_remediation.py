"""A quality finding as a security case, closed only on a passed re-test (agentic_os/products/remediation.py).

The Sentinel end of the contract is tested against the real service in apps/edge-sentinel/test_api_v1.py. Here: what Projects sends, what it
re-reads before it mints an approval, and every way a resolution is refused before a token exists.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_os.mission.approval import verify_approval_token
from agentic_os.products import remediation as R
from agentic_os.products.client import ProductError

SECRET = "approval-secret-for-tests-0123456789abcdef"
F, T = "r20261124T100000Z-aaaaaa", "r20261124T110000Z-bbbbbb"
CASE = "case-0123456789abcdef"


def run(rid, **kw):
    base = {"id": rid, "state": "finished", "outcome": "FAILED", "profile": "fixture-browser", "target_id": "tgt-376713304fea", "defect": "none",
            "finished": "2026-11-24T10:00:05Z", "estimate_digest": "sha256:" + "e" * 64}
    return {**base, **kw}


class Qa:
    def __init__(self, runs, verify=None):
        self.runs_, self.verify_, self.started = runs, verify or {}, []

    def run(self, rid, actor="", source=""):
        if rid not in self.runs_:
            raise ProductError("not_found", "unknown run", status=404)
        return dict(self.runs_[rid])

    def verify(self, rid, actor="", source=""):
        return self.verify_.get(rid, {"result": {"accepted": True, "anchor": {"recomputed_manifest_digest": "sha256:" + hashlib.sha256(rid.encode()).hexdigest()}}})

    def evidence(self, rid, actor="", source=""):
        return {"assertions": [{"assertion_id": "A-NOTIF-ON", "native_status": "NATIVE_FAIL"}, {"assertion_id": "A-OK", "native_status": "NATIVE_PASS"}]}

    def estimate(self, target_id, profile, actor="", source=""):
        return {"digest": "sha256:" + "e" * 64, "target_id": target_id, "profile": profile}

    def start_run(self, body, actor="", source=""):
        self.started.append(dict(body))
        return {"id": T, "state": "awaiting_approval", "estimate_digest": "sha256:" + "e" * 64, **body}


class Sentinel:
    def __init__(self):
        self.events, self.resolutions = [], []

    def report_event(self, event, actor="", source=""):
        self.events.append((dict(event), actor))
        return {"case_id": CASE, "status": "INVESTIGATING"}

    def resolve_case(self, case_id, *, evidence, digest, approval_token, actor):
        self.resolutions.append({"case_id": case_id, "evidence": dict(evidence), "digest": digest, "token": approval_token, "actor": actor})
        return {"case_id": case_id, "status": "RESOLVED", "evidence_id": "ev-1"}


class Hub:
    def __init__(self, qa, sn, ready=("redevops-qa", "edge-sentinel")):
        self.q, self.s, self.ready_, self.invalidated = qa, sn, set(ready), []

    def specs(self):
        return [SimpleNamespace(id="edge-sentinel", kind="security", display_name="Edge Sentinel", ui_link=lambda p: p),
                SimpleNamespace(id="redevops-qa", kind="quality", display_name="redevops-qa", ui_link=lambda p: p)]

    def spec(self, pid):
        return next((s for s in self.specs() if s.id == pid), None)

    def qa(self, pid):
        return self.q

    def sentinel(self, pid):
        return self.s

    def ready(self, pid):
        return pid in self.ready_

    def unavailable_reason(self, pid):
        return {"reason": "down", "remedy": "start it"}

    def invalidate(self, pid=""):
        self.invalidated.append(pid)

    def client(self, pid):
        return SimpleNamespace(approval_secret=lambda: SECRET)


def hub(runs=None, **kw):
    return Hub(Qa(runs or {F: run(F), T: run(T, outcome="VERIFIED", finished="2026-11-24T11:00:05Z")}, **kw), Sentinel())


def test_a_run_that_did_not_pass_becomes_one_event_that_names_the_run_and_its_evidence():
    h = hub()
    out = R.escalate_finding(h, "redevops-qa", F, severity="high", principal="operator")
    (event, actor), = h.s.events
    assert event["kind"] == "qa.finding" and event["subject"] == f"qa-run:{F}" and event["source"] == "redevops-qa" and event["severity"] == "high"
    assert event["min_severity"] == "info" and event["ts"] == 1795514405.0 and actor == "projects:operator"
    assert "ended FAILED: A-NOTIF-ON" in event["detail"] and out["manifest_digest"] in event["detail"] and len(event["detail"]) <= 500
    assert set(event) == {"kind", "severity", "subject", "source", "detail", "ts", "min_severity"}      # only fields Sentinel accepts
    assert out["case_id"] == CASE and out["failing_assertions"] == ["A-NOTIF-ON"] and out["security_product"] == "edge-sentinel"


@pytest.mark.parametrize("record,why", [
    (run(F, outcome="VERIFIED"), "no finding"), (run(F, outcome="CORROBORATED"), "no finding"),
    (run(F, state="running", outcome=None), "has ended"), (run(F, state="awaiting_approval", outcome=None), "has ended"),
    (run(F, state="rejected", outcome=None), "tested nothing"), (run(F, state="cancelled", outcome=None), "tested nothing"),
])
def test_only_a_run_that_ended_without_a_pass_is_a_finding(record, why):
    h = hub({F: record})
    with pytest.raises(ProductError) as e:
        R.escalate_finding(h, "redevops-qa", F, principal="operator")
    assert e.value.status == 409 and why in e.value.detail and h.s.events == []


def test_a_run_with_no_bundle_is_escalated_and_says_so():
    h = hub({F: run(F, outcome="NOT_COMPLETED")}, verify={F: {"result": {}}})
    h.q.evidence = lambda *a, **k: (_ for _ in ()).throw(ProductError("not_found", "no bundle", status=404))
    out = R.escalate_finding(h, "redevops-qa", F, severity="medium", principal="operator")
    assert out["manifest_digest"] == "" and "no evidence bundle was produced" in h.s.events[0][0]["detail"]


def test_escalation_needs_both_products_and_a_known_severity():
    with pytest.raises(ProductError) as e:
        R.escalate_finding(Hub(Qa({F: run(F)}), Sentinel(), ready=("redevops-qa",)), "redevops-qa", F, principal="operator")
    assert e.value.code == "unavailable"
    with pytest.raises(ProductError):
        R.escalate_finding(hub(), "redevops-qa", F, severity="info", principal="operator")


def test_the_retest_repeats_the_target_and_profile_and_never_a_seeded_defect():
    h = hub({F: run(F, defect="settings")})
    out = R.start_retest(h, "redevops-qa", F, principal="operator")
    assert h.q.started == [{"target_id": "tgt-376713304fea", "profile": "fixture-browser", "estimate_digest": "sha256:" + "e" * 64}]
    assert out["retest_run_id"] == T and out["state"] == "awaiting_approval" and out["approval"]


def test_a_passed_retest_closes_the_case_with_a_single_use_approval_bound_to_the_case_and_the_record():
    h = hub()
    out = R.resolve_case(h, "edge-sentinel", CASE, finding_run_id=F, retest_run_id=T, principal="operator")
    sent, = h.s.resolutions
    ev = sent["evidence"]
    assert ev == {"kind": "qa.retest", "run_id": T, "outcome": "VERIFIED", "finding_run_id": F, "profile": "fixture-browser",
                  "target_id": "tgt-376713304fea", "manifest_digest": ev["manifest_digest"]} and ev["manifest_digest"].startswith("sha256:")
    assert sent["digest"] == R.resolution_digest(CASE, ev) == hashlib.sha256(
        json.dumps({"case_id": CASE, "evidence": ev}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ok = verify_approval_token(SECRET, sent["token"], operator="edge-sentinel", capability="sentinel.resolve_case",
                               inputs={"case_id": CASE, "digest": sent["digest"]})
    assert ok and ok["approved"] is True and ok["by"] == "projects:operator"
    for other in ({"case_id": "case-ffffffffffffffff", "digest": sent["digest"]}, {"case_id": CASE, "digest": "0" * 64}):
        assert verify_approval_token(SECRET, sent["token"], operator="edge-sentinel", capability="sentinel.resolve_case", inputs=other) is None
    assert verify_approval_token(SECRET, sent["token"], operator="edge-sentinel", capability="sentinel.decide",
                                 inputs={"case_id": CASE, "digest": sent["digest"]}) is None
    assert out["case_status"] == "RESOLVED" and out["retest"] == ev


@pytest.mark.parametrize("retest,verify,why", [
    (run(T, outcome="FAILED"), None, "ended FAILED"),
    (run(T, outcome="NOT_COMPLETED"), None, "ended NOT COMPLETED"),
    (run(T, state="running", outcome=None), None, "ended running"),
    (run(T, state="awaiting_approval", outcome=None), None, "ended awaiting approval"),
    (run(T, outcome="VERIFIED", profile="fixture-robot-not-enforced"), None, "different profile"),
    (run(T, outcome="VERIFIED", target_id="tgt-000000000000"), None, "different target"),
    (run(T, outcome="VERIFIED", defect="settings"), None, "seeded defect"),
    (run(T, outcome="VERIFIED"), {"result": {"accepted": False, "anchor": {"recomputed_manifest_digest": "sha256:" + "1" * 64}}}, "did not verify"),
    (run(T, outcome="VERIFIED"), {"result": {"accepted": True, "anchor": {}}}, "did not verify"),
])
def test_no_approval_is_minted_unless_the_retest_is_a_verified_pass_of_the_same_target_and_profile(retest, verify, why):
    h = hub({F: run(F), T: retest}, verify={T: verify} if verify else None)
    with pytest.raises(ProductError) as e:
        R.resolve_case(h, "edge-sentinel", CASE, finding_run_id=F, retest_run_id=T, principal="operator")
    assert e.value.status == 409 and why in e.value.detail and h.s.resolutions == []


def test_the_finding_run_cannot_be_its_own_retest():
    h = hub({F: run(F, outcome="VERIFIED")})
    with pytest.raises(ProductError) as e:
        R.resolve_case(h, "edge-sentinel", CASE, finding_run_id=F, retest_run_id=F, principal="operator")
    assert "new run" in e.value.detail and h.s.resolutions == []


def test_the_digest_is_the_one_edge_sentinel_recomputes():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
    live = importlib.import_module("edge-sentinel.live")
    ev = {"kind": "qa.retest", "run_id": T, "outcome": "VERIFIED", "manifest_digest": "sha256:" + "2" * 64, "finding_run_id": F,
          "profile": "fixture-browser", "target_id": "tgt-376713304fea"}
    assert R.resolution_digest(CASE, ev) == live.resolution_digest(CASE, live.clean_resolution(ev))
    assert set(ev) <= set(live.RESOLUTION_FIELDS)                 # Projects sends nothing Sentinel would refuse as unknown


def test_the_three_routes_are_human_only():
    from agentic_os.products.routes import PRODUCT_ROUTE_POLICIES as P
    for route in ("POST /api/products/{product_id}/runs/{run_id}/escalation", "POST /api/products/{product_id}/runs/{run_id}/retest",
                  "POST /api/products/{product_id}/cases/{case_id}/resolution"):
        assert P[route].access == "human_only"
