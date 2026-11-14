"""A case an operator opens by hand: "ban this address" or "lift this ban", staged for the same approval and read-back as any alert.

The request itself (who, when, which address, why) is stored as immutable, content-addressed evidence, so the case can be
rebuilt from it after a restart: there is no CrowdSec alert to derive it from. Nothing here acts: the action is AWAITING_APPROVAL.
"""
from __future__ import annotations

from .evidence import (ActionRequest, ActionState, CaseStatus, CaseType, EvidenceArtifact, Finding, FindingStatus, SecurityCase,
                       SecurityObservation, Severity, canonical_json)

KINDS = {"block": "sentinel.block_ip", "unblock": "sentinel.unblock_ip"}


def request_record(kind: str, ip: str, *, reason: str, duration: str, by: str, at: str) -> dict:
    rec = {"kind": kind, "ip": ip, "reason": reason, "by": by, "at": at}
    if kind == "block":
        rec["duration"] = duration
    return rec


def ingest(store, rec: dict) -> SecurityCase:
    """Deterministic and idempotent: the same request record yields the same ids, so a rebuild after a restart does not duplicate."""
    kind, ip = rec["kind"], rec["ip"]
    raw = canonical_json(rec)
    ev = store.put_evidence(EvidenceArtifact.of_raw("manual_request", "edge-sentinel", raw, observed_at=rec["at"]), raw)
    title = f"Manual {kind}: {ip} ({rec['reason']})"[:160]
    obs = store.put_observation(SecurityObservation(
        source="edge-sentinel", source_type="manual", observed_at=rec["at"], known_at=ev.known_at, raw_evidence_ref=ev.artifact_id,
        normalized_fields={"kind": f"manual_{kind}", "source_ip": ip, "scenario": rec["reason"], "requested_by": rec["by"]},
        network_refs=(f"ip:{ip}",), confidence=1.0))
    capability = KINDS[kind]
    finding = store.put_finding(Finding(
        claim=f"{rec['by']} asked to {kind} {ip}: {rec['reason']}", confidence=1.0, evidence_refs=(ev.artifact_id,),
        recommended_actions=(capability,), status=FindingStatus.SUPPORTED))
    params = {"ip": ip, "reason": rec["reason"]}
    if kind == "block":
        params["duration"] = rec["duration"]
    proposed = ActionRequest(capability=capability, parameters=params, finding_refs=(finding.finding_id,), evidence_refs=(ev.artifact_id,),
                             approval_required=True, state=ActionState.AWAITING_APPROVAL)
    req = store.actions.get(proposed.request_id) or store.put_action(proposed)
    case = SecurityCase(case_type=CaseType.INCIDENT, severity=Severity("medium"), created_at=rec["at"], known_at=ev.known_at, title=title,
                        status=CaseStatus.AWAITING_APPROVAL, observation_refs=(obs.id,), evidence_refs=(ev.artifact_id,),
                        finding_refs=(finding.finding_id,), action_refs=(req.request_id,))
    return store.cases.get(case.case_id) or store.put_case(case)
