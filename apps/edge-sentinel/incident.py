"""Incident spine — turn a real CrowdSec alert/decision into a replayable SecurityCase (Phase 1).

This is the Phase 1 acceptance path: an existing CrowdSec alert becomes a case with raw evidence, a
normalized observation, an evidence-backed finding, and (through approval) a decision, action request and
receipt — and the whole thing replays deterministically from the stored raw evidence.

The normalization reuses the SAME severity mapping the live dashboard uses (``core._severity``), so the
case model and the UI agree. Nothing here talks to CrowdSec; it consumes the dicts ``core.fetch_alerts``
/ ``core.fetch_decisions`` already return, so it is pure and testable against fixtures.
"""
from __future__ import annotations

from .evidence import (
    ActionReceipt,
    ActionRequest,
    ActionState,
    CaseStatus,
    CaseType,
    EvidenceArtifact,
    Finding,
    FindingStatus,
    SecurityCase,
    SecurityDecision,
    SecurityObservation,
    Severity,
    canonical_json,
    sha256_hex,
)

try:
    from .core import _severity as _crowdsec_severity
except Exception:  # pragma: no cover - core imports httpx; keep the spine importable without it
    def _crowdsec_severity(scenario: str) -> str:
        s = (scenario or "").lower()
        if any(k in s for k in ("bruteforce", "brute", "credential", "exploit", "rce", "malware", "c2")):
            return "critical"
        if any(k in s for k in ("probing", "traversal", "injection", "http-", "web")):
            return "high"
        if any(k in s for k in ("scan", "nmap", "port")):
            return "medium"
        return "low"


def _alert_source_ip(alert: dict) -> str:
    src = alert.get("source") or {}
    return src.get("value") or src.get("ip") or alert.get("value") or "?"


def normalize_crowdsec_alert(alert: dict, evidence: EvidenceArtifact) -> SecurityObservation:
    """Lift a raw CrowdSec alert dict into a typed observation. Pure function of (alert, evidence)."""
    scenario = alert.get("scenario", "")
    ip = _alert_source_ip(alert)
    src = alert.get("source") or {}
    created = alert.get("created_at", "") or evidence.acquired_at
    return SecurityObservation(
        source="crowdsec", source_type="crowdsec", observed_at=created, known_at=evidence.known_at,
        raw_evidence_ref=evidence.artifact_id,
        normalized_fields={
            "scenario": scenario,
            "source_ip": ip,
            "scope": src.get("scope", "Ip"),
            "events_count": alert.get("events_count", 0),
            "severity": _crowdsec_severity(scenario),
        },
        network_refs=(f"ip:{ip}",) if ip and ip != "?" else (),
        confidence=1.0)


def _finding_from_observation(obs: SecurityObservation, evidence: EvidenceArtifact) -> Finding:
    nf = obs.normalized_fields
    ip, scenario, sev = nf["source_ip"], nf["scenario"], nf["severity"]
    return Finding(
        claim=f"Source {ip} matched CrowdSec scenario '{scenario}' (severity {sev}).",
        confidence=obs.confidence,
        evidence_refs=(evidence.artifact_id,),
        affected_assets=(),
        recommended_actions=("sentinel.block_ip",) if sev in ("critical", "high") else (),
        status=FindingStatus.SUPPORTED)


def ingest_crowdsec_alert(store, alert: dict) -> SecurityCase:
    """The ingestion path. Returns an INCIDENT case linking immutable raw evidence → observation →
    finding, plus a PROPOSED (approval-gated) block_ip ActionRequest when the finding recommends it.
    Idempotent: re-ingesting the same alert yields the same content-addressed ids and does not duplicate."""
    raw = canonical_json(alert)
    ev = store.put_evidence(EvidenceArtifact.of_raw("crowdsec_alert", "crowdsec", raw,
                                                    observed_at=alert.get("created_at", "")), raw)
    obs = store.put_observation(normalize_crowdsec_alert(alert, ev))
    finding = store.put_finding(_finding_from_observation(obs, ev))
    sev = Severity(obs.normalized_fields["severity"])

    action_refs = ()
    if "sentinel.block_ip" in finding.recommended_actions:
        ip = obs.normalized_fields["source_ip"]
        req = store.put_action(ActionRequest(
            capability="sentinel.block_ip", parameters={"ip": ip},
            finding_refs=(finding.finding_id,), evidence_refs=(ev.artifact_id,),
            approval_required=True, state=ActionState.AWAITING_APPROVAL))
        action_refs = (req.request_id,)

    case = SecurityCase(
        case_type=CaseType.INCIDENT, severity=sev,
        created_at=obs.observed_at or ev.known_at, known_at=ev.known_at,
        title=f"CrowdSec: {obs.normalized_fields['scenario']} from {obs.normalized_fields['source_ip']}",
        status=CaseStatus.AWAITING_APPROVAL if action_refs else CaseStatus.INVESTIGATING,
        observation_refs=(obs.id,), evidence_refs=(ev.artifact_id,),
        finding_refs=(finding.finding_id,), action_refs=action_refs)
    return store.put_case(case)


def record_response(store, case: SecurityCase, request_id: str, *, actor: str, approved: bool,
                    external_ref: str = "", error: str = "") -> tuple[SecurityDecision, ActionReceipt | None]:
    """Record the governed response trail on a case: the human SecurityDecision on the exact ActionRequest,
    and — only if approved — an ActionReceipt (distinct object; execution proof, not the decision). This is
    the decision/action-request/receipt half of the Phase 1 acceptance criterion.

    The receipt is NOT verification: verifying the ban actually took effect is a separate step (Phase 7)."""
    req = store.actions[request_id]
    decision = store.put_decision(SecurityDecision(request_id=request_id, actor=actor, approved=approved,
                                                   rationale=("approved" if approved else "rejected")))
    case.decision_refs = tuple(dict.fromkeys(case.decision_refs + (decision.decision_id,)))
    receipt = None
    if approved:
        store.put_action(ActionRequest(  # advance the request state (new immutable version)
            capability=req.capability, parameters=req.parameters, finding_refs=req.finding_refs,
            evidence_refs=req.evidence_refs, approval_required=req.approval_required,
            state=ActionState.APPROVED))
        status = "SUCCEEDED" if not error else "FAILED"
        receipt = store.put_receipt(ActionReceipt(request_id=request_id, decision_id=decision.decision_id,
                                                  status=status, external_ref=external_ref, error=error))
        case.status = CaseStatus.CONTAINED if status == "SUCCEEDED" else case.status
    else:
        case.status = CaseStatus.INVESTIGATING
    store.put_case(case)
    return decision, receipt


def replay_case(store, alert: dict) -> dict:
    """Deterministic replay: re-run ingestion of the SAME raw alert into a FRESH store and prove the
    content-addressed identities are identical (evidence sha256, observation id, finding id, case id).
    This is the 'replayable case' guarantee — the narrative can change, the evidence-derived ids cannot."""
    from .case_store import CaseStore
    fresh = CaseStore()
    case2 = ingest_crowdsec_alert(fresh, alert)
    return {
        "case_id": case2.case_id,
        "evidence_ids": sorted(fresh.evidence.keys()),
        "observation_ids": sorted(fresh.observations.keys()),
        "finding_ids": sorted(fresh.findings.keys()),
    }
