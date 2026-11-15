"""The evidence-chain bridge: a completed deploy-scan result becomes a replayable EXPOSURE case, the
same contract test_evidence_case.py proves for a CrowdSec alert."""
from __future__ import annotations

import importlib

case_store = importlib.import_module("edge-sentinel.case_store")
evidence = importlib.import_module("edge-sentinel.evidence")
exposure_scan = importlib.import_module("edge-sentinel.exposure_scan")

CaseStore = case_store.CaseStore
CaseStatus = evidence.CaseStatus
CaseType = evidence.CaseType
FindingStatus = evidence.FindingStatus
Severity = evidence.Severity

# the shape apps/deploy-scan's results() / summarize() actually produce
SCAN_RESULT = {
    "job_id": "abc123",
    "state": "done",
    "summary": {"target": "my-ctx/my-ns", "total": 2, "by_severity": {"critical": 1, "medium": 1}, "max_severity": "critical"},
    "findings": [
        {"severity": "critical", "name": "container 'app' runs privileged", "target": "my-ns/pod-1",
         "ref": "app", "detail": "...", "recommended_action": "Drop privileged."},
        {"severity": "medium", "name": "pod uses the 'default' ServiceAccount, which IS bound to RBAC",
         "target": "my-ns/pod-2", "ref": "default", "detail": "...", "recommended_action": "Give it its own SA."},
    ],
}

CLEAN_RESULT = {"job_id": "clean1", "state": "done",
                "summary": {"target": "t", "total": 0, "by_severity": {}, "max_severity": ""}, "findings": []}


def test_a_scan_with_findings_opens_a_replayable_exposure_case():
    store = CaseStore()
    case = exposure_scan.open_exposure_case(store, SCAN_RESULT, target="my-ctx/my-ns", profile="k8s-exposure")

    assert case.case_type is CaseType.EXPOSURE
    assert case.severity is Severity.CRITICAL          # derived from summary.max_severity
    assert case.status is CaseStatus.INVESTIGATING
    assert len(case.finding_refs) == 2
    assert len(case.observation_refs) == 2
    assert len(case.evidence_refs) == 1                # both observations derive from ONE raw-result artifact

    # replayable: every observation and finding is actually retrievable from the store by the ids the case holds
    for obs_id in case.observation_refs:
        assert store.observations[obs_id].source_type == "runtime"
    claims = {store.findings[fid].claim for fid in case.finding_refs}
    assert "container 'app' runs privileged" in claims
    assert store.cases[case.case_id] is case


def test_a_clean_scan_still_opens_a_case_with_no_findings():
    store = CaseStore()
    case = exposure_scan.open_exposure_case(store, CLEAN_RESULT, target="t", profile="web-baseline")
    assert case.status is CaseStatus.OPEN
    assert case.finding_refs == ()
    assert case.severity is Severity.INFO


def test_the_same_raw_scan_result_is_the_same_evidence_if_ingested_twice():
    store = CaseStore()
    c1 = exposure_scan.open_exposure_case(store, SCAN_RESULT, target="my-ctx/my-ns", profile="k8s-exposure")
    c2 = exposure_scan.open_exposure_case(store, SCAN_RESULT, target="my-ctx/my-ns", profile="k8s-exposure")
    assert c1.evidence_refs == c2.evidence_refs        # content-addressed: identical raw result -> identical artifact id
