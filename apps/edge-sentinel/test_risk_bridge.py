"""Phase 8 acceptance: no invented financial inputs; missing assumptions explicit; executive report
(the handoff) traces to technical evidence. Edge Sentinel produces the typed facts; Compliance owns calc.
"""
from __future__ import annotations

import importlib

import pytest

incident = importlib.import_module("edge-sentinel.incident")
case_store = importlib.import_module("edge-sentinel.case_store")
risk_bridge = importlib.import_module("edge-sentinel.risk_bridge")
evidence = importlib.import_module("edge-sentinel.evidence")

CaseStore = case_store.CaseStore
ingest_crowdsec_alert = incident.ingest_crowdsec_alert
build_risk_translation = risk_bridge.build_risk_translation
emit_handoff = risk_bridge.emit_handoff
handoff_traces_to_evidence = risk_bridge.handoff_traces_to_evidence
RiskHandoffError = risk_bridge.RiskHandoffError
RiskTranslation = evidence.RiskTranslation

ALERT = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "203.0.113.7", "scope": "Ip"},
         "events_count": 12, "created_at": "2026-09-21T10:00:00Z"}


def _case_with_finding():
    s = CaseStore()
    case = ingest_crowdsec_alert(s, ALERT)   # produces an evidence-backed finding
    return s, case


def test_handoff_is_evidence_backed_and_technical_only():
    s, case = _case_with_finding()
    handoff = emit_handoff(s, case, affected_business_service="edge-gateway",
                           scenario="external brute-force against the SSH edge",
                           likelihood_basis="repeated failed auths from a single source, escalating",
                           impact_basis="unauthorized shell on an internet-facing host",
                           assumptions=("the source is not a known scanner",),
                           uncertainty="single-source; no lateral movement observed yet")
    rt = handoff.risk_translation
    assert rt.fair_inputs is None                                 # NEVER a financial input
    assert rt.technical_finding_refs and rt.evidence_refs
    assert handoff.target_app == "agentic-compliance"
    assert handoff_traces_to_evidence(s, handoff) is True         # traces to real evidence
    # the payload carries only descriptive bases, no numbers Edge invented
    d = handoff.to_dict()
    assert d["risk_translation"]["likelihood_basis"] and "fair_inputs" in d["risk_translation"]


def test_edge_sentinel_cannot_invent_financial_impact():
    with pytest.raises(ValueError):
        RiskTranslation(technical_finding_refs=("fnd-1",), fair_inputs={"loss": 100000})


def test_missing_assumptions_are_explicit_not_silent():
    s, case = _case_with_finding()
    handoff = emit_handoff(s, case, affected_business_service="edge-gateway",
                           scenario="brute force", likelihood_basis="repeated failures",
                           impact_basis="account compromise", assumptions=())   # none supplied
    assert handoff.assumptions_stated is False                    # explicit, not hidden
    assert handoff.to_dict()["assumptions_stated"] is False


def test_refuses_to_translate_a_case_with_no_findings():
    """A case with no findings has nothing evidence-backed to translate — refuse rather than emit a hollow
    risk statement."""
    CaseType = evidence.CaseType
    Severity = evidence.Severity
    SecurityCase = evidence.SecurityCase
    s = CaseStore()
    empty = s.put_case(SecurityCase(case_type=CaseType.INCIDENT, severity=Severity.LOW,
                                    created_at="t", known_at="t", title="nothing yet"))
    with pytest.raises(RiskHandoffError):
        build_risk_translation(s, empty, affected_business_service="x", scenario="y",
                               likelihood_basis="z", impact_basis="w")
