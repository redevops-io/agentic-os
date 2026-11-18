"""Runtime-exposure scanning as an Edge Sentinel case — the evidence-chain bridge for deploy_scan.py.

deploy_scan.py's scan()/status()/results() talk to the deploy-scan service and return chat-tool-shaped
dicts; they do not themselves open a case. This module takes a completed scan's raw result (the dict
`deploy_scan.results()` returns) and instruments it the same way ai_security.py instruments a runtime
event sequence: the whole raw JSON becomes one immutable EvidenceArtifact, each scanner finding becomes
an evidence-backed SecurityObservation + Finding, and the set opens (or the caller folds it into) an
EXPOSURE case — so a deploy-scan run shows up in Edge Sentinel's case list/UI like everything else,
replayable from its evidence, same as a CrowdSec- or Wazuh-sourced case.
"""
from __future__ import annotations

from .evidence import (
    CaseStatus,
    CaseType,
    EvidenceArtifact,
    Finding,
    FindingStatus,
    SecurityCase,
    SecurityObservation,
    Severity,
    _now,
)

#: deploy-scan's finding severities (findings.py in apps/deploy-scan) map onto Edge Sentinel's Severity
#: by name — both use the same five-level scale.
_SEVERITY = {s.value: s for s in Severity}


def observations_from_scan(store, scan_result: dict, *, target: str, profile: str) -> list[SecurityObservation]:
    """Instrument one deploy-scan result dict as evidence-backed observations — one per finding, all
    deriving from the same raw-result EvidenceArtifact (so the whole scan is replayable from one blob)."""
    raw = scan_result
    ev = store.put_evidence(
        EvidenceArtifact.of_raw("deploy_scan_result", f"deploy-scan:{profile}", raw), raw)
    observations = []
    for finding in (scan_result.get("findings") or []):
        observations.append(store.put_observation(SecurityObservation(
            source=f"deploy-scan:{profile}", source_type="runtime",
            observed_at=ev.acquired_at, known_at=ev.known_at, raw_evidence_ref=ev.artifact_id,
            normalized_fields=finding, network_refs=(f"target:{finding.get('target', target)}",))))
    return observations


def findings_from_observations(observations: list[SecurityObservation]) -> list[Finding]:
    """One Finding per observation, carrying the scanner's own severity, claim and recommended action
    straight through — deploy-scan's scanners already produce an evidence-backed claim; this just gives
    it Edge Sentinel's Finding shape so it can live in a case."""
    out = []
    for obs in observations:
        nf = obs.normalized_fields
        severity_name = nf.get("severity", "info")
        actions = (nf["recommended_action"],) if nf.get("recommended_action") else ()
        out.append(Finding(
            claim=nf.get("name", "deploy-scan finding"),
            confidence=0.9, evidence_refs=(obs.raw_evidence_ref,),
            affected_assets=(f"target:{nf.get('target', '')}",),
            recommended_actions=actions,
            status=FindingStatus.SUPPORTED if severity_name != "info" else FindingStatus.PROPOSED))
    return out


def _case_severity(findings: list[Finding], scan_result: dict) -> Severity:
    max_sev = (scan_result.get("summary") or {}).get("max_severity", "")
    return _SEVERITY.get(max_sev, Severity.INFO)


def open_exposure_case(store, scan_result: dict, *, target: str, profile: str,
                        title: str = "") -> SecurityCase:
    """Open an EXPOSURE case from one completed deploy-scan run. A clean scan (no findings) still opens
    an OPEN case with no findings — a clean result is itself evidence, not nothing to record."""
    observations = observations_from_scan(store, scan_result, target=target, profile=profile)
    findings = findings_from_observations(observations)
    for f in findings:
        store.put_finding(f)
    case = SecurityCase(
        case_type=CaseType.EXPOSURE, severity=_case_severity(findings, scan_result),
        created_at=_now(), known_at=_now(),
        title=title or f"Runtime exposure scan ({profile}) of {target}",
        status=CaseStatus.INVESTIGATING if findings else CaseStatus.OPEN,
        asset_refs=(f"target:{target}",),
        observation_refs=tuple(o.id for o in observations),
        evidence_refs=tuple(dict.fromkeys(o.raw_evidence_ref for o in observations)),
        finding_refs=tuple(f.finding_id for f in findings))
    return store.put_case(case)
