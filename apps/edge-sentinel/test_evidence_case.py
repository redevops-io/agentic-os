"""Phase 1 acceptance: an existing CrowdSec alert becomes a REPLAYABLE case with raw evidence, a
normalized observation, a finding, and (through approval) a decision, action request and receipt.

Also pins the spine's invariants: evidence is immutable + content-addressed, findings can't be CONFIRMED
without supporting evidence, and Edge Sentinel refuses to invent FAIR/financial inputs.
"""
from __future__ import annotations

import importlib

import pytest

# The app is the implicit namespace package `edge-sentinel` (run with apps/ on PYTHONPATH), so its
# modules load via importlib — the same convention as test_operator.py.
evidence = importlib.import_module("edge-sentinel.evidence")
case_store = importlib.import_module("edge-sentinel.case_store")
incident = importlib.import_module("edge-sentinel.incident")

ActionState = evidence.ActionState
CaseStatus = evidence.CaseStatus
CaseType = evidence.CaseType
EvidenceArtifact = evidence.EvidenceArtifact
Finding = evidence.Finding
FindingStatus = evidence.FindingStatus
RiskTranslation = evidence.RiskTranslation
Severity = evidence.Severity
canonical_json = evidence.canonical_json
CaseStore = case_store.CaseStore
IntegrityError = case_store.IntegrityError
ingest_crowdsec_alert = incident.ingest_crowdsec_alert
record_response = incident.record_response
replay_case = incident.replay_case

# a realistic CrowdSec alert dict (the shape core.fetch_alerts returns)
ALERT = {
    "scenario": "crowdsecurity/ssh-bf",
    "source": {"value": "203.0.113.7", "scope": "Ip"},
    "events_count": 12,
    "created_at": "2026-09-21T10:00:00Z",
    "message": "12 failed ssh authentications",
}


def test_crowdsec_alert_becomes_a_full_case():
    """The headline acceptance path — raw evidence → observation → finding → case, all linked."""
    s = CaseStore()
    case = ingest_crowdsec_alert(s, ALERT)

    assert case.case_type is CaseType.INCIDENT and case.severity is Severity.CRITICAL   # ssh-bf → critical

    # raw evidence: immutable, content-addressed, verbatim raw kept, custody recorded
    assert len(case.evidence_refs) == 1
    ev = s.evidence[case.evidence_refs[0]]
    assert ev.artifact_id.startswith("ev-") and ev.sha256 and ev.chain_of_custody
    assert s.get_raw(ev) is not None                       # the raw bytes are stored verbatim

    # normalized observation derived from that evidence
    obs = s.observations[case.observation_refs[0]]
    assert obs.raw_evidence_ref == ev.artifact_id
    assert obs.normalized_fields["source_ip"] == "203.0.113.7"
    assert obs.network_refs == ("ip:203.0.113.7",)

    # evidence-backed finding
    fnd = s.findings[case.finding_refs[0]]
    assert fnd.evidence_refs == (ev.artifact_id,)
    assert "203.0.113.7" in fnd.claim and fnd.status is FindingStatus.SUPPORTED
    assert "sentinel.block_ip" in fnd.recommended_actions   # critical → recommends block


def test_action_request_decision_and_receipt():
    """A critical finding raises an approval-gated block_ip; approval yields a decision AND a distinct
    receipt (receipt ≠ decision), and the case advances to CONTAINED."""
    s = CaseStore()
    case = ingest_crowdsec_alert(s, ALERT)
    assert case.status is CaseStatus.AWAITING_APPROVAL and len(case.action_refs) == 1

    req = s.actions[case.action_refs[0]]
    assert req.capability == "sentinel.block_ip" and req.parameters == {"ip": "203.0.113.7"}
    assert req.approval_required and req.state is ActionState.AWAITING_APPROVAL

    decision, receipt = record_response(s, case, req.request_id, actor="soc-lead", approved=True,
                                        external_ref="crowdsec-decision-991")
    assert decision.approved and decision.request_id == req.request_id
    assert receipt is not None and receipt.decision_id == decision.decision_id
    assert receipt.request_id == req.request_id and receipt.status == "SUCCEEDED"
    assert receipt.receipt_id != decision.decision_id      # distinct objects
    assert case.status is CaseStatus.CONTAINED

    bundle = s.case_bundle(case.case_id)
    assert bundle["decisions"] and bundle["receipts"] and bundle["actions"]


def test_rejected_action_records_a_decision_but_no_receipt():
    s = CaseStore()
    case = ingest_crowdsec_alert(s, ALERT)
    decision, receipt = record_response(s, case, case.action_refs[0], actor="soc-lead", approved=False)
    assert not decision.approved and receipt is None
    assert case.status is CaseStatus.INVESTIGATING


def test_case_is_replayable_from_evidence():
    """Re-ingesting the SAME alert into a fresh store yields identical content-addressed ids — the
    'replayable case' guarantee. The narrative may change; evidence-derived identity cannot."""
    s = CaseStore()
    case = ingest_crowdsec_alert(s, ALERT)
    r = replay_case(s, ALERT)
    assert r["case_id"] == case.case_id
    assert r["evidence_ids"] == sorted(s.evidence.keys())
    assert r["observation_ids"] == sorted(s.observations.keys())
    assert r["finding_ids"] == sorted(s.findings.keys())


def test_ingestion_is_idempotent():
    """The same alert twice does not duplicate evidence/observations/findings (content-addressed)."""
    s = CaseStore()
    ingest_crowdsec_alert(s, ALERT)
    ingest_crowdsec_alert(s, ALERT)
    assert len(s.evidence) == 1 and len(s.observations) == 1 and len(s.findings) == 1 and len(s.cases) == 1


def test_a_low_severity_alert_opens_a_case_without_an_action():
    s = CaseStore()
    low = {**ALERT, "scenario": "crowdsecurity/http-probing"}   # 'probing' → high; use a scan for medium
    low = {**ALERT, "scenario": "crowdsecurity/port-scan"}
    case = ingest_crowdsec_alert(s, low)
    assert case.severity is Severity.MEDIUM
    assert case.action_refs == () and case.status is CaseStatus.INVESTIGATING   # no auto block recommended


# ── spine invariants ──

def test_evidence_is_immutable_and_content_addressed():
    ev = EvidenceArtifact.of_raw("crowdsec_alert", "crowdsec", ALERT)
    s = CaseStore()
    s.put_evidence(ev, canonical_json(ALERT))
    # tampering with the stored bytes under the same content_ref is rejected
    with pytest.raises(IntegrityError):
        s.put_evidence(ev, canonical_json({**ALERT, "scenario": "tampered"}))


def test_finding_cannot_be_confirmed_without_supporting_evidence():
    assert Finding(claim="x", confidence=0.9, evidence_refs=(), status=FindingStatus.CONFIRMED).validate() is False
    assert Finding(claim="x", confidence=0.9, evidence_refs=("ev-1",), status=FindingStatus.CONFIRMED).validate() is True
    # contradiction outweighing support blocks confirmation
    assert Finding(claim="x", confidence=0.9, evidence_refs=("ev-1",),
                   contradicting_evidence_refs=("ev-2", "ev-3"), status=FindingStatus.CONFIRMED).validate() is False


def test_edge_sentinel_must_not_invent_financial_impact():
    """RiskTranslation is a handoff; Edge Sentinel supplies basis + evidence, never a fabricated number."""
    rt = RiskTranslation(technical_finding_refs=("fnd-1",), affected_business_service="edge-gateway",
                         likelihood_basis="repeated ssh brute force from a single source",
                         evidence_refs=("ev-1",))
    assert rt.fair_inputs is None
    with pytest.raises(ValueError):
        RiskTranslation(technical_finding_refs=("fnd-1",), fair_inputs={"loss_magnitude": 50000})
